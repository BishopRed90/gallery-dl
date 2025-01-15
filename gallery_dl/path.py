# -*- coding: utf-8 -*-

# Copyright 2021-2023 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Filesystem path handling"""

import os
from pathlib import Path
import re
import shutil
import functools
from typing import TextIO

from . import util, formatter, exception

WINDOWS = util.WINDOWS
EXTENSION_MAP = {
    "jpeg": "jpg",
    "jpe":  "jpg",
    "jfif": "jpg",
    "jif":  "jpg",
    "jfi":  "jpg",
    }
RESTRICT_MAP = {
    "auto":    "\\\\|/<>:\"?*" if WINDOWS else "/",
    "unix":    "/",
    "windows": "\\\\|/<>:\"?*",
    "ascii":   "^0-9A-Za-z_.",
    "ascii+":  "^0-9@-[\\]-{ #-)+-.;=!}~",
    }
STRIP_MAP = {
    "auto":    ". " if WINDOWS else "",
    "unix":    "",
    "windows": ". ",
    }
DEFAULT_BASEDIR = "." + os.sep + "gallery-dl" + os.sep


class PathFormat:

    def __init__(self, extractor):
        # Init - Config Variables
        config = extractor.config
        directory_fmt: list[str] | dict = config("directory", extractor.directory_fmt)
        filename_fmt: str | dict = config("filename", extractor.filename_fmt)
        kwdefault = config("keywords-default", util.NONE)

        # Path Formatting - Static Characters
        self.extension_map = (config("extension-map") or EXTENSION_MAP).get
        self.strip = self._parse_strip(config("path-strip", "auto"))
        self.restrict = self._parse_restrict(config("path-restrict", "auto"))
        self.replace = config("path-replace", "_")
        self.remove = config("path-remove", "\x00-\x1f\x7f")

        # Function Alias
        self.clean_path = self._build_clean(self.remove, "")
        self.clean_segment = self._build_clean(self.restrict, self.replace)

        self.kwdict = {}
        self.delete = False
        self.prefix = ""
        self.filename = ""
        self.extension = ""

        # Directory Information
        self.base_directory = Path(self._parse_base_dir(config("base-directory")))
        self.file_directory = Path()
        self.directory = ""
        self.realdirectory = ""

        # Path Information
        self.path = ""
        self.realpath = ""
        self.temppath = ""
        self.extended = config("path-extended", True) if WINDOWS else False

        # Filename Formatters
        self._filename_formatters: list[tuple] = self._parse_filename_formats(extractor, kwdefault, filename_fmt)
        self._directory_formatters: list[tuple] = self._parse_directory_formats(extractor, kwdefault, directory_fmt)

    # # Property Setup
    # @property
    # def base_directory(self):
    #     pass
    #
    # @base_directory.setter
    # def base_directory(self, value):
    #     pass
    #
    # @base_directory.deleter
    # def base_directory(self):
    #     pass




    # Parsers/Internal Functions
    @staticmethod
    def _parse_restrict(restrict_type: str = "auto") -> str:
        """
        Extracts and returns the appropriate restrict pattern for a given type.
        """
        return RESTRICT_MAP.get(restrict_type, "/")

    @staticmethod
    def _parse_strip(strip_type: str = "auto") -> str:
        """
        Extracts and returns the appropriate strip characters for a given type.
        """
        return STRIP_MAP.get(strip_type, "")

    def _parse_base_dir(self, base_dir: str = None) -> str:
        """
        Prepares the base directory by either setting a default or cleaning it up
        Args:
            base_dir: Path String of the base starting directory to use.

        Returns:
            Cleaned up base directory string.
        """
        sep = os.sep
        alt_sep = os.altsep

        if base_dir is None:
            base_dir = DEFAULT_BASEDIR
        else:
            base_dir = util.expand_path(base_dir)
            if alt_sep and alt_sep in base_dir:
                base_dir = base_dir.replace(alt_sep, sep)
            if base_dir.endswith(sep):
                base_dir += sep

        return self.clean_path(base_dir)

    @staticmethod
    def _parse_filename_formats(extractor, keywords, formats: dict | list | str):
        try:
            formatters: list[tuple] = []
            if isinstance(formats, str):
                # If the format is a string just add it as a parser
                formatters = [
                    (None, formatter.parse(formats, keywords).format_map)
                    ]
            elif isinstance(formats, dict):
                formatters += [
                    (util.compile_filter(_condition),
                     formatter.parse(_format, keywords).format_map)
                    for _condition, _format in formats.items() if _condition
                    ]

                formatters.append(
                        (None, formatter.parse(formats.get("", extractor.filename_fmt), keywords).format_map)
                        )
            # Make sure None Conditions are at the end
            formatters.sort(key=lambda x: x[0] is None)
            return formatters

        except Exception as exc:
            raise exception.FilenameFormatError(exc)

    @staticmethod
    def _parse_directory_formats(extractor, keywords, formats: dict | list | str):
        try:
            formatters: list[tuple] = []
            if isinstance(formats, (list, set, tuple)):
                # If the format is a string just add it as a parser
                formatters = [
                    (None, [formatter.parse(_format, keywords).format_map for _format in formats])
                    ]
            elif isinstance(formats, dict):
                formatters = [
                    (util.compile_filter(_condition), [
                        formatter.parse(_format, keywords).format_map
                        for _format in _formats])
                    for _condition, _formats in formats.items() if _condition
                    ]

                formatters.append(
                        (None, [formatter.parse(_format, keywords).format_map
                                for _format in formats.get("", extractor.directory_fmt)])
                        )
            # Make sure None Conditions are at the end
            formatters.sort(key=lambda x: x[0] is None)
            return formatters

        except Exception as exc:
            raise exception.DirectoryFormatError(exc)

    @staticmethod
    def _build_clean(chars: str | dict, repl: str):
        if not chars:
            return util.identity
        elif isinstance(chars, dict):
            def func(x, table=str.maketrans(chars)):
                return x.translate(table)
        elif len(chars) == 1:
            def func(x, c=chars, r=repl):
                return x.replace(c, r)
        else:
            return functools.partial(
                    re.compile("[" + chars + "]").sub, repl)
        return func

    def _enum_file(self) -> bool:
        # TODO - this needs be fixed so it will actually enum the file
        # TODO - Currently it calls the build path too early and it get a `none` filename
        num = 1
        try:
            while True:
                prefix = format(num) + "."
                self.kwdict["extension"] = prefix + self.extension
                self.build_path()
                os.stat(self.realpath)  # raises OSError if file doesn't exist
                num += 1
        except OSError:
            pass
        self.prefix = prefix
        return False

    @staticmethod
    def _extended_path(path):
        # TODO - Figure out what the purpose of this code is
        # Enable longer-than-260-character paths
        path = os.path.abspath(path)
        if not path.startswith("\\\\"):
            path = "\\\\?\\" + path
        elif not path.startswith("\\\\?\\"):
            path = "\\\\?\\UNC\\" + path[2:]

        # abspath() in Python 3.7+ removes trailing path separators (#402)
        if path[-1] != os.sep:
            return path + os.sep
        return path

    # File Functions
    def exists(self):
        """Return True if the file exists on disk"""
        if self.extension and os.path.isfile(self.realpath):
            return True
        return False

    def open(self, mode: str = "wb") -> TextIO:
        """Open file and return a corresponding file object"""
        try:
            return open(self.temppath, mode)
        except FileNotFoundError:
            os.makedirs(self.realdirectory)
            return open(self.temppath, mode)

    # Setter Functions
    def set_directory(self, kwdict):
        """Build directory path and create it if necessary"""
        self.kwdict = kwdict

        segments = self.build_directory(kwdict)
        if segments:
            self.directory = directory = os.path.join(self.base_directory, self.clean_path(os.sep.join(segments)))
        else:
            self.directory = directory = self.base_directory

        if WINDOWS and self.extended:
            directory = self._extended_path(directory)
        self.realdirectory = directory

    def set_filename(self, kwdict: dict):
        """Set general filename data"""
        self.kwdict = kwdict
        self.filename = self.temppath = self.prefix = ""

        ext = kwdict["extension"]
        kwdict["extension"] = self.extension = self.extension_map(ext, ext)

    def set_extension(self, extension):
        """Set filename extension"""
        self.extension = extension = self.extension_map(extension, extension)
        self.kwdict["extension"] = self.prefix + extension

    def fix_extension(self, _=None):
        """Fix filenames without a given filename extension"""
        try:
            if not self.extension:
                self.kwdict["extension"] = \
                    self.prefix + self.extension_map("", "")
                self.build_path()
                if self.path[-1] == ".":
                    self.path = self.path[:-1]
                    self.temppath = self.realpath = self.realpath[:-1]
            elif not self.temppath:
                self.build_path()
        except exception.GalleryDLException:
            raise
        except Exception:
            self.path = self.directory + "?"
            self.realpath = self.temppath = self.realdirectory + "?"
        return True

    def build_filename(self, kwdict) -> str:
        try:
            for condition, fmt in self._filename_formatters:
                if condition is None or condition(kwdict):
                    break
            else:
                raise exception.FilenameFormatError("No filename format matched")
            return self.clean_path(self.clean_segment(fmt(kwdict)))
        except Exception as exc:
            raise exception.FilenameFormatError(exc)

    def build_directory(self, kwdict):
        segments = []
        append = segments.append
        strip = self.strip

        try:
            for condition, formatters in self._directory_formatters:
                if condition is None or condition(kwdict):
                    break
            else:
                raise exception.DirectoryFormatError(exception)
            for fmt in formatters:
                segment = fmt(kwdict).strip()
                if strip and segment != "..":
                    segment = segment.rstrip(strip)
                if segment:
                    append(self.clean_segment(segment))
            return segments
        except Exception as exc:
            raise exception.DirectoryFormatError(exc)

    def build_path(self):
        """Combine directory and filename to full paths"""
        self.filename = self.build_filename(self.kwdict)
        self.path = os.path.join(self.directory, self.filename)
        self.realpath = os.path.join(self.realdirectory, self.filename)
        if not self.temppath:
            self.temppath = self.realpath

    def part_enable(self, part_directory=None):
        """Enable .part file usage"""
        if self.extension:
            self.temppath += ".part"
        else:
            self.kwdict["extension"] = self.prefix + self.extension_map(
                    "part", "part")
            self.build_path()
        if part_directory:
            self.temppath = os.path.join(
                    part_directory,
                    os.path.basename(self.temppath),
                    )

    def part_size(self):
        """Return size of .part file"""
        try:
            return os.stat(self.temppath).st_size
        except OSError:
            pass
        return 0

    def finalize(self):
        """Move tempfile to its target location"""
        self.set_directory(self.kwdict)
        self.build_path()
        if self.delete:
            self.delete = False
            os.unlink(self.temppath)
            return
        if self.temppath != self.realpath:
            # Move temp file to its actual location
            while True:
                try:
                    os.replace(self.temppath, self.realpath)
                except FileNotFoundError:
                    # delayed directory creation
                    os.makedirs(self.realdirectory)
                    continue
                except OSError:
                    # move across different filesystems
                    try:
                        shutil.copyfile(self.temppath, self.realpath)
                    except FileNotFoundError:
                        os.makedirs(self.realdirectory)
                        shutil.copyfile(self.temppath, self.realpath)
                    os.unlink(self.temppath)
                break

        mtime = self.kwdict.get("_mtime")
        if mtime:
            util.set_mtime(self.realpath, mtime)

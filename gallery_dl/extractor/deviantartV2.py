# -*- coding: utf-8 -*-

# Copyright 2015-2023 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://www.deviantart.com/"""
from dataclasses import dataclass, Field
import pydantic
from pydantic import BaseModel

from ..Models.Deviantart import *
from .common import Extractor, Message
from .. import text, util, exception
from ..cache import cache, memcache
import collections
import mimetypes
import binascii
import time
import re

BASE_PATTERN = (
    r"(?:https?://)?(?:"
    r"(?:www\.)?(?:fx)?deviantart\.com/(?!watch/)([\w-]+)|"
    r"(?!www\.)([\w-]+)\.(?:fx)?deviantart\.com)"
)
DEFAULT_AVATAR = "https://a.deviantart.net/avatars/default.gif"

class DeviantartExtractor(Extractor):
    """Base class for deviantart extractors"""
    category = "deviantart"
    root = "https://www.deviantart.com"
    directory_fmt = ("{category}", "{username}")
    filename_fmt = "{category}_{index}_{title}.{extension}"
    cookies_domain = ".deviantart.com"
    cookies_names = ("auth", "auth_secure", "userinfo")
    _last_request = 0

    def __init__(self, match):
        Extractor.__init__(self, match)
        self.user = (match.group(1) or match.group(2) or "").lower()
        self.offset = 0

    def _init(self):
        self.jwt = self.config("jwt", False)
        self.flat = self.config("flat", True)
        self.extra = self.config("extra", False)
        self.quality = self.config("quality", "100")
        self.original = self.config("original", True)
        self.previews = self.config("previews", False)
        self.intermediary = self.config("intermediary", True)
        self.comments_avatars = self.config("comments-avatars", False)
        self.comments = self.comments_avatars or self.config("comments", False)

        self.eclipse_api = DeviantartEclipseAPI(self)
        self.api = self.eclipse_api
        self.group = False
        self._premium_cache = {}

        unwatch = self.config("auto-unwatch")
        if unwatch:
            self.unwatch = []
            self.finalize = self._unwatch_premium
        else:
            self.unwatch = None

        if self.quality:
            if self.quality == "png":
                self.quality = "-fullview.png?"
                self.quality_sub = re.compile(r"-fullview\.[a-z0-9]+\?").sub
            else:
                self.quality = ",q_{}".format(self.quality)
                self.quality_sub = re.compile(r",q_\d+").sub

        if isinstance(self.original, str) and \
                self.original.lower().startswith("image"):
            self.original = True
            self._update_content = self._update_content_image
        else:
            self._update_content = self._update_content_default

        if self.previews == "all":
            self.previews_images = self.previews = True
        else:
            self.previews_images = False

        journals = self.config("journals", "html")
        if journals == "html":
            self.commit_journal = self._commit_journal_html
        elif journals == "text":
            self.commit_journal = self._commit_journal_text
        else:
            self.commit_journal = None

    def request(self, url, **kwargs):
        if "fatal" not in kwargs:
            kwargs["fatal"] = False
        while True:
            response = Extractor.request(self, url, **kwargs)
            if response.status_code != 403 or \
                    b"Request blocked." not in response.content:
                return response
            self.wait(seconds=300, reason="CloudFront block")

    def skip(self, num):
        self.offset += num
        return num

    def login(self):
        if self.cookies_check(self.cookies_names):
            return True

        username, password = self._get_auth_info()
        if username:
            self.cookies_update(_login_impl(self, username, password))
            return True

    def _eclipse_deviation(self, deviation):

        if deviation["isDeleted"]:
            # prevent crashing in case the deviation really is
            # deleted
            self.log.debug(
                "Skipping %s (deleted)", deviation["deviationid"])
            return

        if deviation.get("isMultiImage") and not deviation.get("extended"):
            deviation["_extractor"] = DeviantartDeviationExtractor
            yield Message.Queue, deviation["url"], deviation
            return

        if deviation.get('tierAccess') == "locked":
            self.log.debug(
                "Skipping %s (access locked)", deviation["deviationid"])
            return

        if deviation.get("pcp"):
            data = self._fetch_premium(deviation)
            if not data:
                return
            deviation.update(data)

        self.prepare(deviation)
        yield Message.Directory, deviation


        if deviation["isDownloadable"] and deviation.get("uuid"):
            content = self.oauth_api.deviation_download(deviation["uuid"])
            deviation["is_original"] = True
            yield self.commit(deviation, content)
        elif deviation["media"]:
            media = self._extract_media(deviation)
            yield self.commit(deviation, media)
        elif "content" in deviation:
            content = self._extract_content(deviation)
            yield self.commit(deviation, content)

        if "videos" in deviation and deviation["videos"]:
            video = max(deviation["videos"],
                        key=lambda x: text.parse_int(x["quality"][:-1]))
            deviation["is_original"] = False
            yield self.commit(deviation, video)

        if "flash" in deviation:
            deviation["is_original"] = True
            yield self.commit(deviation, deviation["flash"])

        if self.commit_journal:
            journal = self._extract_journal(deviation)
            if journal:
                if self.extra:
                    deviation["_journal"] = journal["html"]
                deviation["is_original"] = True
                yield self.commit_journal(deviation, journal)

        # if self.comments_avatars:
        #     for comment in deviation["comments"]:
        #         user = comment["user"]
        #         name = user["username"].lower()
        #         if user["usericon"] == DEFAULT_AVATAR:
        #             self.log.debug(
        #                 "Skipping avatar of '%s' (default)", name)
        #             continue
        #         _user_details.update(name, user)
        #
        #         url = "{}/{}/avatar/".format(self.root, name)
        #         comment["_extractor"] = DeviantartAvatarExtractor
        #         yield Message.Queue, url, comment

        if self.previews and "preview" in deviation:
            preview = deviation["preview"]
            deviation["is_preview"] = True
            if self.previews_images:
                yield self.commit(deviation, preview)
            else:
                mtype = mimetypes.guess_type(
                    "a." + deviation["extension"], False)[0]
                if mtype and not mtype.startswith("image/"):
                    yield self.commit(deviation, preview)
            del deviation["is_preview"]


        # if not self.extra:
        #     return
        #
        # # ref: https://www.deviantart.com
        # #      /developers/http/v1/20210526/object/editor_text
        # # the value of "features" is a JSON string with forward
        # # slashes escaped
        # text_content = \
        #     deviation["text_content"]["body"]["features"].replace(
        #         "\\/", "/") if "text_content" in deviation else None
        #
        # for txt in (text_content, deviation.get("description"),
        #             deviation.get("_journal")):
        #     if txt is None:
        #         continue
        #     for match in DeviantartStashExtractor.pattern.finditer(txt):
        #         url = text.ensure_http_scheme(match.group(0))
        #         deviation["_extractor"] = DeviantartStashExtractor
        #         yield Message.Queue, url, deviation

    def items(self):
        if self.user:
            group = self.config("group", True)
            if group:
                user = _user_details(self, self.user)
                if user:
                    self.user = user["username"]
                    self.group = False
                elif group == "skip":
                    self.log.info("Skipping group '%s'", self.user)
                    raise exception.StopExtraction()
                else:
                    self.subcategory = "group-" + self.subcategory
                    self.group = True

        for deviation in self.deviations():
            if isinstance(deviation, tuple):
                url, data = deviation
                yield Message.Queue, url, data
                continue

            if isinstance(self.api, DeviantartEclipseAPI):
                yield from self._eclipse_deviation(deviation)

    def deviations(self):
        """Return an iterable containing all relevant Deviation-objects"""

    def prepare(self, deviation):
        """Adjust the contents of a Deviation-object"""
        if "index" not in deviation:
            try:
                if deviation["url"].startswith((
                    "https://www.deviantart.com/stash/", "https://sta.sh",
                )):
                    filename = deviation["content"]["src"].split("/")[5]
                    deviation["index_base36"] = filename.partition("-")[0][1:]
                    deviation["index"] = id_from_base36(
                        deviation["index_base36"])
                else:
                    deviation["index"] = text.parse_int(
                        deviation["url"].rpartition("-")[2])
            except KeyError:
                deviation["index"] = 0
                deviation["index_base36"] = "0"
        if "index_base36" not in deviation:
            deviation["index_base36"] = base36_from_id(deviation["index"])

        if self.user:
            deviation["username"] = self.user
            deviation["_username"] = self.user.lower()
        else:
            deviation["username"] = deviation["author"]["username"]
            deviation["_username"] = deviation["username"].lower()

        deviation["published_time"] = text.parse_int(
            deviation.get("published_time", deviation.get("publishedTime")))
        deviation["date"] = text.parse_timestamp(
            deviation["published_time"])

        if self.comments:
            deviation["comments"] = (
                self._extract_comments(deviation["deviationid"], "deviation")
                if deviation["stats"]["comments"] else ()
            )

        # filename metadata
        sub = re.compile(r"\W").sub
        deviation["filename"] = "".join((
            sub("_", deviation["title"].lower()), "_by_",
            sub("_", deviation["author"]["username"].lower()), "-d",
            deviation["index_base36"],
        ))

    @staticmethod
    def commit(deviation, target):
        url = target["src"]
        name = target.get("filename") or url
        target = target.copy()
        target["filename"] = deviation["filename"]
        deviation["target"] = target
        deviation["extension"] = target["extension"] = text.ext_from_url(name)
        if "is_original" not in deviation:
            deviation["is_original"] = ("/v1/" not in url)
        return Message.Url, url, deviation

    def _commit_journal_html(self, deviation, journal):
        title = text.escape(deviation["title"])
        url = deviation["url"]
        thumbs = deviation.get("thumbs") or deviation.get("files")
        html = journal["html"]
        shadow = SHADOW_TEMPLATE.format_map(thumbs[0]) if thumbs else ""

        if not html:
            self.log.warning("%s: Empty journal content", deviation["index"])

        if "css" in journal:
            css, cls = journal["css"], "withskin"
        elif html.startswith("<style"):
            css, _, html = html.partition("</style>")
            css = css.partition(">")[2]
            cls = "withskin"
        else:
            css, cls = "", "journal-green"

        if html.find('<div class="boxtop journaltop">', 0, 250) != -1:
            needle = '<div class="boxtop journaltop">'
            header = HEADER_CUSTOM_TEMPLATE.format(
                title=title, url=url, date=deviation["date"],
            )
        else:
            needle = '<div usr class="gr">'
            username = deviation["author"]["username"]
            urlname = deviation.get("username") or username.lower()
            header = HEADER_TEMPLATE.format(
                title=title,
                url=url,
                userurl="{}/{}/".format(self.root, urlname),
                username=username,
                date=deviation["date"],
            )

        if needle in html:
            html = html.replace(needle, header, 1)
        else:
            html = JOURNAL_TEMPLATE_HTML_EXTRA.format(header, html)

        html = JOURNAL_TEMPLATE_HTML.format(
            title=title, html=html, shadow=shadow, css=css, cls=cls)

        deviation["extension"] = "htm"
        return Message.Url, html, deviation

    def _commit_journal_text(self, deviation, journal):
        html = journal["html"]
        if not html:
            self.log.warning("%s: Empty journal content", deviation["index"])
        elif html.startswith("<style"):
            html = html.partition("</style>")[2]
        head, _, tail = html.rpartition("<script")
        content = "\n".join(
            text.unescape(text.remove_html(txt))
            for txt in (head or tail).split("<br />")
        )
        txt = JOURNAL_TEMPLATE_TEXT.format(
            title=deviation["title"],
            username=deviation["author"]["username"],
            date=deviation["date"],
            content=content,
        )

        deviation["extension"] = "txt"
        return Message.Url, txt, deviation

    def _extract_journal(self, deviation):
        if "excerpt" in deviation:
            # # empty 'html'
            #  return self.api.deviation_content(deviation["deviationid"])

            if "_page" in deviation:
                page = deviation["_page"]
                del deviation["_page"]
            else:
                page = self._limited_request(deviation["url"]).text

            # extract journal html from webpage
            html = text.extr(
                page,
                "<h2>Literature Text</h2></span><div>",
                "</div></section></div></div>")
            if html:
                return {"html": html}

            self.log.debug("%s: Failed to extract journal HTML from webpage. "
                           "Falling back to __INITIAL_STATE__ markup.",
                           deviation["index"])

            # parse __INITIAL_STATE__ as fallback
            state = util.json_loads(text.extr(
                page, 'window.__INITIAL_STATE__ = JSON.parse("', '");')
                .replace("\\\\", "\\").replace("\\'", "'").replace('\\"', '"'))
            deviations = state["@@entities"]["deviation"]
            content = deviations.popitem()[1]["textContent"]

            html = self._textcontent_to_html(deviation, content)
            if html:
                return {"html": html}
            return {"html": content["excerpt"].replace("\n", "<br />")}

        if "body" in deviation:
            return {"html": deviation.pop("body")}
        return None

    def _textcontent_to_html(self, deviation, content):
        html = content["html"]
        markup = html.get("markup")

        if not markup or markup[0] != "{":
            return markup

        if html["type"] == "tiptap":
            try:
                return self._tiptap_to_html(markup)
            except Exception as exc:
                self.log.debug("", exc_info=exc)
                self.log.error("%s: '%s: %s'", deviation["index"],
                               exc.__class__.__name__, exc)

        self.log.warning("%s: Unsupported '%s' markup.",
                         deviation["index"], html["type"])

    def _tiptap_to_html(self, markup):
        html = []

        html.append('<div data-editor-viewer="1" '
                    'class="_83r8m _2CKTq _3NjDa mDnFl">')
        data = util.json_loads(markup)
        for block in data["document"]["content"]:
            self._tiptap_process_content(html, block)
        html.append("</div>")

        return "".join(html)

    def _tiptap_process_content(self, html, content):
        type = content["type"]

        if type == "paragraph":
            children = content.get("content")
            if children:
                html.append('<p style="')

                attrs = content["attrs"]
                if "textAlign" in attrs:
                    html.append("text-align:")
                    html.append(attrs["textAlign"])
                    html.append(";")
                self._tiptap_process_indentation(html, attrs)
                html.append('">')

                for block in children:
                    self._tiptap_process_content(html, block)
                html.append("</p>")
            else:
                html.append('<p class="empty-p"><br/></p>')

        elif type == "text":
            self._tiptap_process_text(html, content)

        elif type == "heading":
            attrs = content["attrs"]
            level = str(attrs.get("level") or "3")

            html.append("<h")
            html.append(level)
            html.append(' style="text-align:')
            html.append(attrs.get("textAlign") or "left")
            html.append('">')
            html.append('<span style="')
            self._tiptap_process_indentation(html, attrs)
            html.append('">')
            self._tiptap_process_children(html, content)
            html.append("</span></h")
            html.append(level)
            html.append(">")

        elif type in ("listItem", "bulletList", "orderedList", "blockquote"):
            c = type[1]
            tag = (
                "li" if c == "i" else
                "ul" if c == "u" else
                "ol" if c == "r" else
                "blockquote"
            )
            html.append("<" + tag + ">")
            self._tiptap_process_children(html, content)
            html.append("</" + tag + ">")

        elif type == "anchor":
            attrs = content["attrs"]
            html.append('<a id="')
            html.append(attrs.get("id") or "")
            html.append('" data-testid="anchor"></a>')

        elif type == "hardBreak":
            html.append("<br/><br/>")

        elif type == "horizontalRule":
            html.append("<hr/>")

        elif type == "da-deviation":
            self._tiptap_process_deviation(html, content)

        elif type == "da-mention":
            user = content["attrs"]["user"]["username"]
            html.append('<a href="https://www.deviantart.com/')
            html.append(user.lower())
            html.append('" data-da-type="da-mention" data-user="">@<!-- -->')
            html.append(user)
            html.append('</a>')

        elif type == "da-gif":
            attrs = content["attrs"]
            width = str(attrs.get("width") or "")
            height = str(attrs.get("height") or "")
            url = text.escape(attrs.get("url") or "")

            html.append('<div data-da-type="da-gif" data-width="')
            html.append(width)
            html.append('" data-height="')
            html.append(height)
            html.append('" data-alignment="')
            html.append(attrs.get("alignment") or "")
            html.append('" data-url="')
            html.append(url)
            html.append('" class="t61qu"><video role="img" autoPlay="" '
                        'muted="" loop="" style="pointer-events:none" '
                        'controlsList="nofullscreen" playsInline="" '
                        'aria-label="gif" data-da-type="da-gif" width="')
            html.append(width)
            html.append('" height="')
            html.append(height)
            html.append('" src="')
            html.append(url)
            html.append('" class="_1Fkk6"></video></div>')

        elif type == "da-video":
            src = text.escape(content["attrs"].get("src") or "")
            html.append('<div data-testid="video" data-da-type="da-video" '
                        'data-src="')
            html.append(src)
            html.append('" class="_1Uxvs"><div data-canfs="yes" data-testid="v'
                        'ideo-inner" class="main-video" style="width:780px;hei'
                        'ght:438px"><div style="width:780px;height:438px">'
                        '<video src="')
            html.append(src)
            html.append('" style="width:100%;height:100%;" preload="auto" cont'
                        'rols=""></video></div></div></div>')

        else:
            self.log.warning("Unsupported content type '%s'", type)

    def _tiptap_process_text(self, html, content):
        marks = content.get("marks")
        if marks:
            close = []
            for mark in marks:
                type = mark["type"]
                if type == "link":
                    attrs = mark.get("attrs") or {}
                    html.append('<a href="')
                    html.append(text.escape(attrs.get("href") or ""))
                    if "target" in attrs:
                        html.append('" target="')
                        html.append(attrs["target"])
                    html.append('" rel="')
                    html.append(attrs.get("rel") or
                                "noopener noreferrer nofollow ugc")
                    html.append('">')
                    close.append("</a>")
                elif type == "bold":
                    html.append("<strong>")
                    close.append("</strong>")
                elif type == "italic":
                    html.append("<em>")
                    close.append("</em>")
                elif type == "underline":
                    html.append("<u>")
                    close.append("</u>")
                elif type == "strike":
                    html.append("<s>")
                    close.append("</s>")
                elif type == "textStyle" and len(mark) <= 1:
                    pass
                else:
                    self.log.warning("Unsupported text marker '%s'", type)
            close.reverse()
            html.append(text.escape(content["text"]))
            html.extend(close)
        else:
            html.append(text.escape(content["text"]))

    def _tiptap_process_children(self, html, content):
        children = content.get("content")
        if children:
            for block in children:
                self._tiptap_process_content(html, block)

    def _tiptap_process_indentation(self, html, attrs):
        itype = ("text-indent" if attrs.get("indentType") == "line" else
                 "margin-inline-start")
        isize = str((attrs.get("indentation") or 0) * 24)
        html.append(itype + ":" + isize + "px")

    def _tiptap_process_deviation(self, html, content):
        dev = content["attrs"]["deviation"]
        media = dev.get("media") or ()

        html.append('<div class="jjNX2">')
        html.append('<figure class="Qf-HY" data-da-type="da-deviation" '
                    'data-deviation="" '
                    'data-width="" data-link="" data-alignment="center">')

        if "baseUri" in media:
            url, formats = self._eclipse_media(media)
            full = formats["fullview"]

            html.append('<a href="')
            html.append(text.escape(dev["url"]))
            html.append('" class="_3ouD5" style="margin:0 auto;display:flex;'
                        'align-items:center;justify-content:center;'
                        'overflow:hidden;width:780px;height:')
            html.append(str(780 * full["h"] / full["w"]))
            html.append('px">')

            html.append('<img src="')
            html.append(text.escape(url))
            html.append('" alt="')
            html.append(text.escape(dev["title"]))
            html.append('" style="width:100%;max-width:100%;display:block"/>')
            html.append("</a>")

        elif "textContent" in dev:
            html.append('<div class="_32Hs4" style="width:350px">')

            html.append('<a href="')
            html.append(text.escape(dev["url"]))
            html.append('" class="_3ouD5">')

            html.append('''\
<section class="Q91qI aG7Yi" style="width:350px;height:313px">\
<div class="_16ECM _1xMkk" aria-hidden="true">\
<svg height="100%" viewBox="0 0 15 12" preserveAspectRatio="xMidYMin slice" \
fill-rule="evenodd">\
<linearGradient x1="87.8481761%" y1="16.3690766%" \
x2="45.4107524%" y2="71.4898596%" id="app-root-3">\
<stop stop-color="#00FF62" offset="0%"></stop>\
<stop stop-color="#3197EF" stop-opacity="0" offset="100%"></stop>\
</linearGradient>\
<text class="_2uqbc" fill="url(#app-root-3)" text-anchor="end" x="15" y="11">J\
</text></svg></div><div class="_1xz9u">Literature</div><h3 class="_2WvKD">\
''')
            html.append(text.escape(dev["title"]))
            html.append('</h3><div class="_2CPLm">')
            html.append(text.escape(dev["textContent"]["excerpt"]))
            html.append('</div></section></a></div>')

        html.append('</figure></div>')

    def _extract_content(self, deviation):
        content = deviation["content"]

        if self.original and deviation["is_downloadable"]:
            self._update_content(deviation, content)
            return content

        if self.jwt:
            self._update_token(deviation, content)
            return content

        if content["src"].startswith("https://images-wixmp-"):
            if self.intermediary and deviation["index"] <= 790677560:
                # https://github.com/r888888888/danbooru/issues/4069
                intermediary, count = re.subn(
                    r"(/f/[^/]+/[^/]+)/v\d+/.*",
                    r"/intermediary\1", content["src"], 1)
                if count:
                    deviation["is_original"] = False
                    deviation["_fallback"] = (content["src"],)
                    content["src"] = intermediary
            if self.quality:
                content["src"] = self.quality_sub(
                    self.quality, content["src"], 1)

        return content

    def _extract_media(self, deviation):
        media = deviation["media"]
        src = media["baseUri"] + "?token="
        token = media["token"][0]
        media["src"] = src + token

        return media

    @staticmethod
    def _find_folder(folders, name, uuid):
        if uuid.isdecimal():
            match = re.compile(name.replace(
                "-", r"[^a-z0-9]+") + "$", re.IGNORECASE).match
            for folder in folders:
                if match(folder["name"]):
                    return folder
                elif folder["has_subfolders"]:
                    for subfolder in folder["subfolders"]:
                        if match(subfolder["name"]):
                            return subfolder
        else:
            for folder in folders:
                if folder["folderid"] == uuid:
                    return folder
                elif folder["has_subfolders"]:
                    for subfolder in folder["subfolders"]:
                        if subfolder["folderid"] == uuid:
                            return subfolder
        raise exception.NotFoundError("folder")

    def _folder_urls(self, folders, category, extractor):
        base = "{}/{}/{}/".format(self.root, self.user, category)
        for folder in folders:
            folder["_extractor"] = extractor
            url = "{}{}/{}".format(base, folder["folderid"], folder["name"])
            yield url, folder

    def _update_content_default(self, deviation, content):
        if "premium_folder_data" in deviation or deviation.get("is_mature"):
            public = False
        else:
            public = None

        data = self.api.deviation_download(deviation["deviationid"], public)
        content.update(data)
        deviation["is_original"] = True

    def _update_content_image(self, deviation, content):
        data = self.api.deviation_download(deviation["deviationid"])
        url = data["src"].partition("?")[0]
        mtype = mimetypes.guess_type(url, False)[0]
        if mtype and mtype.startswith("image/"):
            content.update(data)
            deviation["is_original"] = True

    def _update_token(self, deviation, content):
        """Replace JWT to be able to remove width/height limits

        All credit goes to @Ironchest337
        for discovering and implementing this method
        """
        url, sep, _ = content["src"].partition("/v1/")
        if not sep:
            return

        # 'images-wixmp' returns 401 errors, but just 'wixmp' still works
        url = url.replace("//images-wixmp", "//wixmp", 1)

        #  header = b'{"typ":"JWT","alg":"none"}'
        payload = (
            b'{"sub":"urn:app:","iss":"urn:app:","obj":[[{"path":"/f/' +
            url.partition("/f/")[2].encode() +
            b'"}]],"aud":["urn:service:file.download"]}'
        )

        deviation["_fallback"] = (content["src"],)
        deviation["is_original"] = True
        content["src"] = (
            "{}?token=eyJ0eXAiOiJKV1QiLCJhbGciOiJub25lIn0.{}.".format(
                url,
                #  base64 of 'header' is precomputed as 'eyJ0eX...'
                #  binascii.b2a_base64(header).rstrip(b"=\n").decode(),
                binascii.b2a_base64(payload).rstrip(b"=\n").decode())
        )

    def _extract_comments(self, target_id, target_type="deviation"):
        results = None
        comment_ids = [None]

        while comment_ids:
            comments = self.api.comments(
                target_id, target_type, comment_ids.pop())

            if results:
                results.extend(comments)
            else:
                results = comments

            # parent comments, i.e. nodes with at least one child
            parents = {c["parentid"] for c in comments}
            # comments with more than one reply
            replies = {c["commentid"] for c in comments if c["replies"]}
            # add comment UUIDs with replies that are not parent to any node
            comment_ids.extend(replies - parents)

        return results

    def _limited_request(self, url, **kwargs):
        """Limits HTTP requests to one every 2 seconds"""
        diff = time.time() - DeviantartExtractor._last_request
        if diff < 2.0:
            self.sleep(2.0 - diff, "request")
        response = self.request(url, **kwargs)
        DeviantartExtractor._last_request = time.time()
        return response

    def _fetch_premium(self, deviation):
        try:
            return self._premium_cache[deviation["deviationid"]]
        except KeyError:
            pass

        if not self.api.refresh_token_key:
            self.log.warning(
                "Unable to access premium content (no refresh-token)")
            self._fetch_premium = lambda _: None
            return None

        dev = self.api.deviation(deviation["deviationid"], False)
        folder = deviation["premium_folder_data"]
        username = dev["author"]["username"]

        # premium_folder_data is no longer present when user has access (#5063)
        has_access = ("premium_folder_data" not in dev) or folder["has_access"]

        if not has_access and folder["type"] == "watchers" and \
                self.config("auto-watch"):
            if self.unwatch is not None:
                self.unwatch.append(username)
            if self.api.user_friends_watch(username):
                has_access = True
                self.log.info(
                    "Watching %s for premium folder access", username)
            else:
                self.log.warning(
                    "Error when trying to watch %s. "
                    "Try again with a new refresh-token", username)

        if has_access:
            self.log.info("Fetching premium folder data")
        else:
            self.log.warning("Unable to access premium content (type: %s)",
                             folder["type"])

        cache = self._premium_cache
        for dev in self.api.gallery(
                username, folder["gallery_id"], public=False):
            cache[dev["deviationid"]] = dev if has_access else None

        return cache.get(deviation["deviationid"])

    def _unwatch_premium(self):
        for username in self.unwatch:
            self.log.info("Unwatching %s", username)
            self.api.user_friends_unwatch(username)

    def _eclipse_media(self, media, format="preview"):
        url = [media["baseUri"]]

        formats = {
            fmt["t"]: fmt
            for fmt in media["types"]
        }

        tokens = media.get("token") or ()
        if tokens:
            if len(tokens) <= 1:
                fmt = formats[format]
                if "c" in fmt:
                    url.append(fmt["c"].replace(
                        "<prettyName>", media["prettyName"]))
            url.append("?token=")
            url.append(tokens[-1])

        return "".join(url), formats

    def _eclipse_to_oauth(self, eclipse_api, deviations):
        for obj in deviations:
            deviation = obj["deviation"] if "deviation" in obj else obj
            deviation_uuid = eclipse_api.deviation_extended_fetch(
                deviation["deviationId"],
                deviation["author"]["username"],
                "journal" if deviation["isJournal"] else "art",
            )["deviation"]["extended"]["deviationUuid"]
            yield self.api.deviation(deviation_uuid)

class DeviantartDeviationExtractor(DeviantartExtractor):
    """Extractor for single deviations"""
    subcategory = "deviation"
    archive_fmt = "g_{_username}_{index}.{extension}"
    pattern = (BASE_PATTERN + r"/(art|journal)/(?:[^/?#]+-)?(\d+)"
               r"|(?:https?://)?(?:www\.)?(?:fx)?deviantart\.com/"
               r"(?:view/|deviation/|view(?:-full)?\.php/*\?(?:[^#]+&)?id=)"
               r"(\d+)"  # bare deviation ID without slug
               r"|(?:https?://)?fav\.me/d([0-9a-z]+)")  # base36
    example = "https://www.deviantart.com/USER/art/TITLE-12345"

    skip = Extractor.skip

    def __init__(self, match):
        DeviantartExtractor.__init__(self, match)
        self.type = match.group(3)
        self.deviation_id = \
            match.group(4) or match.group(5) or id_from_base36(match.group(6))

    def deviations(self):
        if isinstance(self.api, DeviantartEclipseAPI):
            _deviation_info = self.eclipse_api.deviation_extended_fetch(self.deviation_id, self.user, self.type)
            deviation = _deviation_info["deviation"]
            deviation["comments"] = _deviation_info["comments"]
            deviation['uuid'] = _deviation_info["deviation"]["extended"].get("deviationUuid")

            if deviation.get("isMultiImage"):
                self.filename_fmt = ("{category}_{index}_{index_file}_{title}_"
                                     "{num:>02}.{extension}")
                self.archive_fmt = ("g_{_username}_{index}{index_file:?_//}."
                                    "{extension}")
                additional_media = deviation["extended"].get("additionalMedia")

                deviation["index_file"] = 0
                deviation["count"] = 1 + len(additional_media)
                deviation["num"] = 1
            else:
                additional_media = []

            yield deviation

            if additional_media:
                for index, post in enumerate(additional_media):
                    deviation["media"] = post["media"]
                    deviation["filename"] = post["filename"]
                    deviation["num"] += 1
                    deviation["index_file"] = post["fileId"]
                    # Download only works on purchased materials - no way to check
                    deviation["isDownloadable"] = False
                    yield deviation

class DeviantartEclipseAPI():
    """Interface to the DeviantArt Eclipse API"""

    def __init__(self, extractor):
        self.extractor = extractor
        self.log = extractor.log
        self.request = self.extractor._limited_request
        self.csrf_token = None

    def subscription(self, tier_deviation_id):
        endpoint = "/_puppy/dashared/tier/content"
        params = {
            "tier_deviationid": tier_deviation_id,
            "da_minor_version": "20230710",
        }
        return self._call(endpoint, params=params)

    def deviation(self, deviation_id, user, kind=None):
        endpoint = "/_puppy/dadeviation/init"
        params = {
            "deviationid"     : deviation_id,
            "username"        : user,
            "type"            : kind,
            "include_session" : "false",
            "expand"          : "deviation.related",
            "da_minor_version": "20230710",
        }
        return self._call(endpoint, params)

    def gallery_scraps(self, user, offset=0):
        endpoint = "/_puppy/dashared/gallection/contents"
        params = {
            "username"     : user,
            "type"         : "gallery",
            "offset"       : offset,
            "limit"        : 24,
            "scraps_folder": "true",
        }
        return self._pagination(endpoint, params)

    def galleries_search(self, user, query, offset=0, order="most-recent"):
        endpoint = "/_puppy/dashared/gallection/search"
        params = {
            "username": user,
            "type"    : "gallery",
            "order"   : order,
            "q"       : query,
            "offset"  : offset,
            "limit"   : 24,
        }
        return self._pagination(endpoint, params)

    def search_deviations(self, params):
        endpoint = "/_puppy/dabrowse/search/deviations"
        return self._pagination(endpoint, params, key="deviations")

    def user_info(self, user, expand=False):
        endpoint = "/_puppy/dauserprofile/init/about"
        params = {"username": user}
        return self._call(endpoint, params)

    def user_watching(self, user, offset=0):
        gruserid, moduleid = self._ids_watching(user)

        endpoint = "/_puppy/gruser/module/watching"
        params = {
            "gruserid"     : gruserid,
            "gruser_typeid": "4",
            "username"     : user,
            "moduleid"     : moduleid,
            "offset"       : offset,
            "limit"        : 24,
        }
        return self._pagination(endpoint, params)

    def deviants_you_watch(self, user_id):
        endpoint = "/_puppy/damessagecentre/stack"
        params = {
            "stackid": "uq:devwatch:tg=deviations,sender={}".format(user_id),
            "type": "deviations",
            "offset": 0,
            "limit": 50, # Doing more, then this resets to 10
        }
        return self._pagination(endpoint, params)

    def _call(self, endpoint, params):
        url = "https://www.deviantart.com" + endpoint
        params["csrf_token"] = self.csrf_token or self._fetch_csrf_token()

        response = self.request(url, params=params, fatal=None)

        try:
            return response.json()
        except Exception:
            return {"error": response.text}

    def _pagination(self, endpoint, params, key="results"):
        limit = params.get("limit", 24)
        warn = True

        while True:
            data = self._call(endpoint, params)

            results = data.get(key)
            if results is None:
                return
            if len(results) < limit and warn and data.get("hasMore"):
                warn = False
                self.log.warning(
                    "Private deviations detected! "
                    "Provide login credentials or session cookies "
                    "to be able to access them.")
            yield from results

            if not data.get("hasMore"):
                return

            if "nextCursor" in data:
                params["offset"] = None
                params["cursor"] = data["nextCursor"]
            elif "nextOffset" in data:
                params["offset"] = data["nextOffset"]
                params["cursor"] = None
            elif params.get("offset") is None:
                return
            else:
                params["offset"] = int(params["offset"]) + len(results)

    def _ids_watching(self, user):
        url = "{}/{}/about".format(self.extractor.root, user)
        page = self.request(url).text

        gruser_id = text.extr(page, ' data-userid="', '"')

        pos = page.find('\\"name\\":\\"watching\\"')
        if pos < 0:
            raise exception.NotFoundError("'watching' module ID")
        module_id = text.rextract(
            page, '\\"id\\":', ',', pos)[0].strip('" ')

        self._fetch_csrf_token(page)
        return gruser_id, module_id

    def _fetch_csrf_token(self, page=None):
        if page is None:
            page = self.request(self.extractor.root + "/").text
        self.csrf_token = token = text.extr(
            page, "window.__CSRF_TOKEN__ = '", "'")
        return token

    def parse_deviation(self, raw_deviation):
        deviation = Deviation(
            raw_deviation['deviationId'],
            raw_deviation['title'],
            raw_deviation
        )


@memcache(keyarg=1)
def _user_details(extr, name):
    try:
        return extr.api.user_profile(name)["user"]
    except Exception:
        return None


@cache(maxage=36500*86400, keyarg=0)
def _refresh_token_cache(token):
    if token and token[0] == "#":
        return None
    return token


@cache(maxage=28*86400, keyarg=1)
def _login_impl(extr, username, password):
    extr.log.info("Logging in as %s", username)

    url = "https://www.deviantart.com/users/login"
    page = extr.request(url).text

    data = {}
    for item in text.extract_iter(page, '<input type="hidden" name="', '"/>'):
        name, _, value = item.partition('" value="')
        data[name] = value

    challenge = data.get("challenge")
    if challenge and challenge != "0":
        extr.log.warning("Login requires solving a CAPTCHA")
        extr.log.debug(challenge)

    data["username"] = username
    data["password"] = password
    data["remember"] = "on"

    extr.sleep(2.0, "login")
    url = "https://www.deviantart.com/_sisu/do/signin"
    response = extr.request(url, method="POST", data=data)

    if not response.history:
        raise exception.AuthenticationError()

    return {
        cookie.name: cookie.value
        for cookie in extr.cookies
    }


def id_from_base36(base36):
    return util.bdecode(base36, _ALPHABET)


def base36_from_id(deviation_id):
    return util.bencode(int(deviation_id), _ALPHABET)


_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


###############################################################################
# Journal Formats #############################################################

SHADOW_TEMPLATE = """
<span class="shadow">
    <img src="{src}" class="smshadow" width="{width}" height="{height}">
</span>
<br><br>
"""

HEADER_TEMPLATE = """<div usr class="gr">
<div class="metadata">
    <h2><a href="{url}">{title}</a></h2>
    <ul>
        <li class="author">
            by <span class="name"><span class="username-with-symbol u">
            <a class="u regular username" href="{userurl}">{username}</a>\
<span class="user-symbol regular"></span></span></span>,
            <span>{date}</span>
        </li>
    </ul>
</div>
"""

HEADER_CUSTOM_TEMPLATE = """<div class='boxtop journaltop'>
<h2>
    <img src="https://st.deviantart.net/minish/gruzecontrol/icons/journal.gif\
?2" style="vertical-align:middle" alt=""/>
    <a href="{url}">{title}</a>
</h2>
Journal Entry: <span>{date}</span>
"""

JOURNAL_TEMPLATE_HTML = """text:<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/deviantart-network_lc.css?3843780832"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/group_secrets_lc.css?3250492874"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/v6core_lc.css?4246581581"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/sidebar_lc.css?1490570941"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/writer_lc.css?3090682151"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/css/v6loggedin_lc.css?3001430805"/>
    <style>{css}</style>
    <link rel="stylesheet" href="https://st.deviantart.net\
/roses/cssmin/core.css?1488405371919"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/roses/cssmin/peeky.css?1487067424177"/>
    <link rel="stylesheet" href="https://st.deviantart.net\
/roses/cssmin/desktop.css?1491362542749"/>
    <link rel="stylesheet" href="https://static.parastorage.com/services\
/da-deviation/2bfd1ff7a9d6bf10d27b98dd8504c0399c3f9974a015785114b7dc6b\
/app.min.css"/>
</head>
<body id="deviantART-v7" class="bubble no-apps loggedout w960 deviantart">
    <div id="output">
    <div class="dev-page-container bubbleview">
    <div class="dev-page-view view-mode-normal">
    <div class="dev-view-main-content">
    <div class="dev-view-deviation">
    {shadow}
    <div class="journal-wrapper tt-a">
    <div class="journal-wrapper2">
    <div class="journal {cls} journalcontrol">
    {html}
    </div>
    </div>
    </div>
    </div>
    </div>
    </div>
    </div>
    </div>
</body>
</html>
"""

JOURNAL_TEMPLATE_HTML_EXTRA = """\
<div id="devskin0"><div class="negate-box-margin" style="">\
<div usr class="gr-box gr-genericbox"
        ><i usr class="gr1"><i></i></i
        ><i usr class="gr2"><i></i></i
        ><i usr class="gr3"><i></i></i
        ><div usr class="gr-top">
            <i usr class="tri"></i>
            {}
            </div>
    </div><div usr class="gr-body"><div usr class="gr">
            <div class="grf-indent">
            <div class="text">
                {}            </div>
        </div>
                </div></div>
        <i usr class="gr3 gb"></i>
        <i usr class="gr2 gb"></i>
        <i usr class="gr1 gb gb1"></i>    </div>
    </div></div>"""

JOURNAL_TEMPLATE_TEXT = """text:{title}
by {username}, {date}

{content}
"""

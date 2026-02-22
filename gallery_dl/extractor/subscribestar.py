# -*- coding: utf-8 -*-

# Copyright 2020-2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://www.subscribestar.com/"""

import inspect
import os.path
import pathlib
from abc import abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Generator, Literal

from bs4 import BeautifulSoup, ResultSet, Tag
from pydantic import BaseModel

from .. import exception, text, util
from ..cache import cache
from .common import Extractor, Message

BASE_PATTERN = r"(?:https?://)?(?:www\.)?subscribestar\.(?P<tld>com|adult)"


# region Dataclasses


class MediaItem(BaseModel):
    url: str
    type: Literal["image", "avatar", "cover", "preview"]
    extension: str | None = None
    filename: str | None = None

    @classmethod
    def from_dict(cls, env):
        return cls(
            **{k: v for k, v in env.items() if k in inspect.signature(cls).parameters}
        )


class PostData(BaseModel):
    author_bio: str
    author_name: str
    author_id: str

    post_date: str
    post_title: str
    post_id: int | None

    tags: list[str]

    @classmethod
    def from_instance(cls, instance, strict: bool = False, **kwargs):
        args = asdict(instance)
        args.update(kwargs)
        if strict:
            return cls(**args)
        else:
            return cls(
                **{
                    k: v
                    for k, v in args.items()
                    if k in inspect.signature(cls).parameters
                }
            )

    @property
    def count(self):
        return len(self.media)


class AvatarItem(MediaItem, PostData):
    user_id: int = None

    @property
    def filename(self):
        return f"{self.type}_{self.user_id}.{self.extension}"


class GalleryItem(PostData, MediaItem):
    id: int
    original_filename: str
    created_at: str
    gallery_preview_url: str
    width: str
    height: str
    num: int = 1


# endregion Dataclasses


class SubscribeStarExtractor(Extractor):
    """Base class for SubscribeStar extractors"""

    category: str = "subscribestar"
    root: str = "https://www.subscribestar.com"
    directory_fmt: tuple = ("{category}", "{author_name}")
    filename_fmt: str = "{post_id}_{id}.{extension}"
    archive_fmt: str = "{id}"
    cookies_domain: str = ".subscribestar.com"
    cookies_names: str = ("_personalization_id",)
    _warning: bool = True

    def __init__(self, match):
        if match.group("tld") == "adult":
            self.root = "https://subscribestar.adult"
            self.cookies_domain = ".subscribestar.adult"
            self.subcategory += "-adult"
            self.soup = BeautifulSoup("", "html.parser")

        Extractor.__init__(self, match)

    def items(self):
        self.login()
        for post_html in self.posts():
            post_data = self._data_from_post(post_html)
            media = self._media_from_post(post_html, post_data)

            yield Message.Directory, "", post_data.model_dump()

            images = []
            for num, item in enumerate(media, 1):
                item.num = num

                if item.url.startswith("/"):
                    item.url = self.root + item.url

                if item.original_filename:
                    item.extension = item.original_filename.split(".")[-1]
                    item.filename = ".".join(item.original_filename.split(".")[:-1])

                # Download the normal image and link
                yield Message.Url, item.url, item.model_dump()
                try:
                    # Attempt to get the previews if the files are too large or they are requested
                    if (
                        self.config("previews")
                        or os.path.getsize(item_dict["_gdl_path"].path) / 1024 / 1024
                        > 4
                    ):
                        # TODO fix this up so less copying
                        item.type = "preview"
                        item_dict = asdict(item)
                        yield Message.Url, item.gallery_preview_url, item_dict
                        item.type = "image"
                        item.filename = item["_gdl_path"].filename

                except FileNotFoundError:
                    pass

                if isinstance(item, GalleryItem):
                    images.append(
                        {
                            "id": item.id,
                            "filename": item.filename,
                            "width": item.width,
                            "height": item.height,
                            "type": item.width,
                        }
                    )

            if self.config("posts", "html") != "html":
                yield self._commit_journal_text(post_data)
            else:
                # 1. Get Post Header
                # TODO - Make this not a separate call if necessary
                post_header = self._get_profile_header(post_data)

                # 2 Handle Post-Body
                body_parts = post_html.find_all(
                    "div", class_=["post-body", "post_tags", "post-actions"]
                )
                post_body = self.soup.new_tag("div", attrs={"class": "section-body"})
                for body_part in body_parts:
                    if body_part.get("class") == ["post-actions"]:
                        comment_container = body_part.find(
                            "div", attrs={"data-role": "post-comments_list"}
                        )
                        post_comments = self._get_post_comments(
                            post_data.get("post_id")
                        )
                        comment_container.append(post_comments)
                    post_body.append(body_part)

                # 3 Build Post
                post_elements = {
                    "post_header": post_header,
                    "post_body": post_body if not None else post_html,
                    "var_filename": var_filename,
                }
                post, post_data = self._build_post_html(post_data, post_elements)

                # 4. Re-Link Avatars
                # TODO - Extra Avatar Work
                yield from self._relink_avatars(post, item if item else post_data)

                # 5 Update Gallery to local values
                if data_gallery := post.find("div", {"class": "uploads-images"}):
                    data_gallery["data-gallery"] = util.json_dumps(media)

                # 6. Re-Link all hrefs back to the website
                for link in post.find_all("a"):
                    link["href"] = self.root + link["href"]

                # 7. Commit Vars
                post_data["extension"] = "js"
                post_data["type"] = "post"
                yield Message.Directory, "", post_data
                avatar_path = pathlib.Path(post_data["_gdl_path"].directory)
                # Path('/usr/var/log').relative_to('/usr/var/log/')
                var_file = HTML_VAR_TEMPLATE.format(
                    post_id=post_data["post_id"],
                    avatar_path="../Avatars/",
                    cover_path="./Header/",
                    image_path=f"../Pictures/{post_data['post_id']}/",
                    preview_path=f"../Pictures/{post_data['post_id']}/Previews/",
                    images=util.json_dumps(images),
                )
                post_data["extension"] = "js"
                post_data["type"] = "post"
                yield Message.Url, var_file, post_data
                var_filename = post_data["_gdl_path"].filename

                # 7. Commit Post
                post_data["extension"] = "html"
                post_data["type"] = "post"
                post_string = f"text:{str(post)}"
                yield Message.Url, post_string, post_data

    @abstractmethod
    def posts(self) -> Generator[Tag, None, None]:
        """Yield HTML content of all relevant posts"""

    def request(self, url, **kwargs):
        while True:
            response = Extractor.request(self, url, **kwargs)

            if response.history and (
                "/verify_subscriber" in response.url
                or "/age_confirmation_warning" in response.url
            ):
                raise exception.AbortExtraction(f"HTTP redirect to {response.url}")

            content = response.content
            if len(content) < 250 and b">redirected<" in content:
                url = text.unescape(text.extr(content, b'href="', b'"').decode())
                self.log.debug("HTML redirect message for %s", url)
                continue

            return response

    def login(self):
        if self.cookies_check(self.cookies_names):
            return

        username, password = self._get_auth_info()
        if username:
            self.cookies_update(
                self._login_impl((username, self.cookies_domain), password)
            )

        if self._warning:
            if not username or not self.cookies_check(self.cookies_names):
                self.log.warning("no '_personalization_id' cookie set")
            SubscribeStarExtractor._warning = False

    @cache(maxage=28 * 86400, keyarg=1)
    def _login_impl(self, username, password):
        username = username[0]
        self.log.info("Logging in as %s", username)

        if self.root.endswith(".adult"):
            self.cookies.set(
                "18_plus_agreement_generic", "true", domain=self.cookies_domain
            )

        # load login page
        url = self.root + "/login"
        page = self.request(url).text

        headers = {
            "Accept": "*/*;q=0.5, text/javascript, application/javascript, "
            "application/ecmascript, application/x-ecmascript",
            "Referer": self.root + "/login",
            "X-CSRF-Token": text.unescape(
                text.extr(page, '<meta name="csrf-token" content="', '"')
            ),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        def check_errors(response):
            if errors := response.json().get("errors"):
                self.log.debug(errors)
                try:
                    msg = f'"{errors.popitem()[1]}"'
                except Exception:
                    msg = None
                raise exception.AuthenticationError(msg)
            return response

        # submit username / email
        url = self.root + "/session.json"
        data = {"email": username}
        response = check_errors(
            self.request(url, method="POST", headers=headers, data=data, fatal=False)
        )

        # submit password
        url = self.root + "/session/password.json"
        data = {"password": password}
        response = check_errors(
            self.request(url, method="POST", headers=headers, data=data, fatal=False)
        )

        # return cookies
        return {cookie.name: cookie.value for cookie in response.cookies}

    def _media_from_post(
        self, post: Tag, post_data: PostData
    ) -> list[GalleryItem | AvatarItem]:
        media = {}
        if gallery := post.find("div", {"class": "uploads-images"}):
            for image in util.json_loads(gallery["data-gallery"]):
                media[image["id"]] = GalleryItem.model_validate(
                    image | post_data.model_dump()
                )

        if attachments := post.find_all("figure", class_="attachment"):
            for attachment in attachments:
                info = util.json_loads(attachment["data-trix-attachment"])
                if info.get("uploadId") is None:
                    continue
                info["url"] = attachment.find("img")["src"]
                info["type"] = "attachment"
                info["id"] = info["uploadId"]
                if not info.get("original_filename"):
                    info["original_filename"] = text.filename_from_url(info["url"])

                # Attachments for photos could not be in the image gallery
                # if so we need to download it
                if not media.get(info["uploadId"]):
                    media[info["uploadId"]] = info

        if audios := text.extr(
            post, 'class="uploads-audios"', 'class="post-edit_form"'
        ):
            for audio in util.re(r'class="audio_preview-data[" ]').split(audios)[1:]:
                media.append(
                    {
                        "id": text.parse_int(text.extr(audio, 'data-upload-id="', '"')),
                        "name": text.unescape(
                            text.extr(audio, 'audio_preview-title">', "<")
                        ),
                        "url": text.unescape(text.extr(audio, 'src="', '"')),
                        "type": "audio",
                    }
                )

        # Avatar and Cover Page
        if self.config("avatars"):
            for element in post.find_all(
                "img",
                {
                    "data-type": ["avatar", "cover"],
                    "src": True,
                },
            ):
                user_id = element["data-user-id"]
                el_type = element["data-type"]

                if avatar := media.get(el_type + user_id):
                    continue
                else:
                    avatar = AvatarItem(
                        **post_data.model_dump(),
                        user_id=int(user_id),
                        type=el_type,
                        extension=text.ext_from_url(element["src"]),
                        url=element["src"],
                    )
                    media[el_type + user_id] = avatar

        return list(media.values())
        # audios = html.find('div', {"class": ["uploads-audios", "post-edit_form"] })
        # # audios = text.extr(
        # #     html, 'class="uploads-audios"', 'class="post-edit_form"')
        # if audios:
        #     for audio in re.split(
        #             r'class="audio_preview-data[" ]', audios)[1:]:
        #         media.append({
        #             "id"  : text.parse_int(text.extr(
        #                 audio, 'data-upload-id="', '"')),
        #             "name": text.unescape(text.extr(
        #                 audio, 'audio_preview-title">', '<')),
        #             "url" : text.unescape(text.extr(audio, 'src="', '"')),
        #             "type": "audio",
        #         })
        return list(media.values())

    def _data_from_post(self, html) -> PostData:
        author_bio = html.find(
            "div", ["star_link-types", "profile_main_info-description"]
        )
        # author_name = html.find(
        #     ["a", "div"], ["profile_main_info-name", "star_link-name", "post-user"]
        # ).text.strip()
        # author_id = html.find(
        #     ["a", "div"], ["star_link-avatar", "post-avatar"]
        # ).next.get("data-user-id")

        # post_date = html.find(["div"], ["section-title_date", "post-date"]).text.strip()
        post_title = html.select("div.post-title") if None else html.find("h1")
        tags = []
        if tag_element := html.find(["div"], ["post_tags"]):
            for tag in tag_element.findChildren("a", recursive=False):
                tags.append(tag.text)
        if _post_id := html.find("[data-post_id]"):
            post_id = _post_id[0]["data-post_id"]
        else:
            post_id = None

        return PostData(
            author_bio=author_bio.text.strip() if author_bio else "",
            author_name=html.find(
                ["a", "div"], ["profile_main_info-name", "star_link-name", "post-user"]
            ).text.strip(),
            author_id=html.find(
                ["a", "div"], ["star_link-avatar", "post-avatar"]
            ).next.get("data-user-id"),
            post_date=html.find(
                ["div"], ["section-title_date", "post-date"]
            ).text.strip(),
            post_title=post_title.text.strip() if post_title else "",
            post_id=post_id,
            tags=tags,
        )
        # return {
        #     "post_id": post_id,
        #     "post_title": post_title.text.strip() if post_title else "",
        #     "author_id": author_id,
        #     "author_name": author_name,
        #     "author_bio": author_bio.text.strip() if author_bio else "",
        #     "date": self._parse_datetime(post_date),
        #     "tags": tags,
        # }

    def _relink_avatars(
        self, post, post_data
    ) -> Generator[tuple[int, Any, Any], Any, None]:
        avatars = {}
        for element in post.find_all(
            "img",
            {
                "data-type": ["avatar", "cover"],
                "src": True,
            },
        ):
            user_id = element["data-user-id"]
            el_type = element["data-type"]
            if avatar := avatars.get(user_id):
                # Just update the avatar with known info
                element["src"][el_type] = avatar
            else:
                avatar_data = post_data.copy()
                avatar_data["type"] = el_type
                avatar_data["url"] = element["src"]
                avatar_data["extension"] = text.ext_from_url(element["src"])
                avatar_data["filename"] = f"{el_type}_{user_id}"
                avatar_data["id"] = user_id
                yield Message.Url, element["src"], avatar_data
                element["src"] = avatars.get(user_id, {})[el_type] = (
                    f"{{{{{el_type}_path}}}}/{avatar_data['_gdl_path'].filename}"
                )

    def _parse_datetime(self, dt):
        if dt.startswith("Updated on "):
            dt = dt[11:]
        date = self.parse_datetime(dt, "%b %d, %Y %I:%M %p")
        if date is dt:
            date = self.parse_datetime(dt, "%B %d, %Y %I:%M %p")
        return date

    def _warn_preview(self):
        self.log.warning("Preview image detected")
        self._warn_preview = util.noop

    def _commit_journal_text(self, post):
        content = post["content"]
        if not content:
            self.log.warning("%s: Empty post content", post["index"])
            return None
        elif content.startswith("<div>") or content.startswith("<p>"):
            content = "\n".join(
                text.remove_html(txt) for txt in content.split("</div>")
            )
        txt = JOURNAL_TEMPLATE_TEXT.format(
            title=post["title"],
            username=post["author_nick"],
            date=post["date"],
            content=content,
        )

        post["extension"] = "txt"
        return Message.Url, txt, post

    def _build_post_html(self, post_data, post_elements):
        if not post_elements["post_body"]:
            self.log.warning("%s: Empty post content", post_data["index"])
            return None
        else:
            html_string = JOURNAL_TEMPLATE_HTML.format(
                post_body=post_elements["post_body"],
                post_date=post_data["date"],
                profile_header=post_elements["post_header"],
                post_title=post_data["post_title"],
                post_id=post_data["post_id"],
                var_filename=post_elements["var_filename"],
            )
            html = BeautifulSoup(html_string, "html.parser")
            return html, post_data

    def _get_post_comments(self, post_id):
        api = self.root + f"/posts/{post_id}/comments.json"
        response = self.request(api)
        raw_data = response.json()
        if not raw_data.get("error"):
            comments = BeautifulSoup(raw_data.get("html", None), "html.parser")
            return comments
        else:
            return None

    @cache()
    def _get_profile_header(self, post_data):
        # TODO - Handle Getting this from a single post
        response = self.request(self.root + f"/{post_data['author_name']}").text
        page = BeautifulSoup(response, "html.parser")
        header = page.find("div", class_="profile_main_info")
        return header


class SubscribeStarUserExtractor(SubscribeStarExtractor):
    """Extractor for media from a subscribestar user"""

    subcategory = "user"
    pattern = BASE_PATTERN + r"/(?!posts/)([^/?#]+)(\?tag=[\w]+)?"
    example = "https://www.subscribestar.com/USER/tag=TAGNAME"

    def posts(self) -> Generator[Tag, None, None]:
        page = self.request(self.url).text

        while True:
            self.soup = BeautifulSoup(page, "html.parser")
            if posts := self.soup.find_all("div", class_="post"):
                yield from posts

                url = self.soup.find("div", {"class": "posts-more"})
                if not url:
                    return
                page = self.request(self.root + text.unescape(url["href"])).json()[
                    "html"
                ]


class SubscribeStarPostExtractor(SubscribeStarExtractor):
    """Extractor for media from a single SubscribeStar post"""

    subcategory = "post"
    pattern = BASE_PATTERN + r"/posts/(?P<post_id>\d+)"
    example = "https://www.subscribestar.com/posts/12345"

    def posts(self) -> ResultSet[Tag]:
        response = self.request(self.url)
        self.soup = BeautifulSoup(response.text, "html.parser")

        return self.soup.select("div.post.wrapper")


# region Templates
JOURNAL_TEMPLATE_TEXT = """text:{title}
by {username}, {date}

{content}
"""

JOURNAL_TEMPLATE_HTML = """
<!DOCTYPE html>
<html lang="">
<head>
    <title>{post_title}</title>
    <meta content="width=device-width, initial-scale=1, maximum-scale=1.0, user-scalable=no" name="viewport"/>
    <meta name="action-cable-url" content="/cable"/>
    <meta content="72c35a3ce4ae8fc1d49ae85a68cb22d5" name="p:domain_verify"/>
    <link rel="stylesheet" media="screen"
          href="../../CSS/SubscribeStar.css"
          data-track-change="true"/>
    <script>
    </script>
</head>
<body>
<div class="layout for-public is-adult" data-role="popup_anchor" data-view="app#layout" id="root">
    <div class="layout-inner" data-view="app#fix_scroll">
        <div class="layout-content">
            {profile_header}
            <div class="post wrapper for-profile_columns is-single is-shown" data-comments-loaded="true"
                 data-edit-path="/posts/1817339/edit?single_post_view=true" data-id="1817339" data-role="popup_anchor"
                 data-view="app#post">
                <div class="section-title">
                    <div class="section-title_date">{post_date}</div>
                </div>
                {post_body}
            </div>
        </div>
    </div>
</div>
<div class="layout-overlay" data-view="app#overlay">
    <div class="overlay-bg" data-role="overlay-bg"></div>
    <div class="overlay" data-scrollable="" data-role="overlay-wrap" tabindex="-1">
        <div class="overlay-close" data-role="overlay-hide">
            <div class="overlay-close_inner">
                <div class="overlay-close_line"></div>
                <div class="overlay-close_line"></div>
            </div>
        </div>
        <div class="overlay-content" data-role="overlay-content" data-view="app#fix_scroll">
        </div>
    </div>
</div>

</body>
<script src="../../CSS/Render.js"></script>
<script src="./{var_filename}"></script>
<script src="../../CSS/Gallery.js"></script>
</html>
"""

HTML_VAR_TEMPLATE = """text:
let config = {{
    post_id: {post_id},
    preview_path: "{preview_path}",
    image_path: "{image_path}",
    avatar_path: "{avatar_path}",
    cover_path: "{cover_path}"
}}
const images = {images}
"""

# endregion Templates

# -*- coding: utf-8 -*-

# Copyright 2020-2025 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://www.subscribestar.com/"""

import datetime
import inspect
import os
from pathlib import Path
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
    name: str | None = None

    @classmethod
    def from_dict(cls, env):
        return cls(
            **{k: v for k, v in env.items() if k in inspect.signature(cls).parameters}
        )

    @property
    def filename(self):
        return f"{self.name}.{self.extension}"


class PostData(BaseModel):
    author: str
    author_bio: str
    author_id: str
    collection_name: str | None = None
    collection_id: int | None = None
    date: datetime.datetime
    post_id: int | None
    title: str
    tags: list[str]
    gallery_count: int = 0
    media_count: int = 0
    previews: bool = False

    # @classmethod
    # def from_instance(cls, instance, strict: bool = False, **kwargs):
    #     args = asdict(instance)
    #     args.update(kwargs)
    #     if strict:
    #         return cls(**args)
    #     else:
    #         return cls(
    #             **{
    #                 k: v
    #                 for k, v in args.items()
    #                 if k in inspect.signature(cls).parameters
    #             }
    #         )


class AvatarItem(MediaItem, PostData):
    user_id: int
    username: str

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
            self.root: str = "https://subscribestar.adult"
            self.cookies_domain: str = ".subscribestar.adult"
            self.subcategory += "-adult"
        Extractor.__init__(self, match)
        self.soup: BeautifulSoup = BeautifulSoup("", "html.parser")
        self.post_config: dict = self.config("posts", {})

    def items(self):
        self.login()
        author_element = None
        for post_html in self.posts():
            post_data = self._data_from_post(post_html)
            if post_html.find("div", class_=["post", "is-locked"]):
                self.log.warning(
                    f"Post is Locked: {post_data.post_id} - {post_data.title}"
                )
                continue
            yield Message.Directory, "", post_data.model_dump()

            if self.post_config.get("style") == "html":
                if self.post_config.get("cover") and author_element is None:
                    author_element = self._get_profile_header(post_data.author)
                    post_template = JOURNAL_HEADER_TEMPLATE_HTML
                elif author_element is None:
                    author_element = self.get_post_sidebar(post_data.post_id)
                    post_template = JOURNAL_SIDEBAR_TEMPLATE_HTML
                else:
                    post_template = JOURNAL_HEADER_TEMPLATE_HTML

                # TODO - How do we get header once/check if user header has been downloaded
                # 1. Get the header for the page if requested

                # 2. Create Post-Body
                if not (post_body := self.get_post_body(post_html, post_data.post_id)):
                    self.log.warning("Post is locked")

                post_elements = {
                    "author_element": author_element,
                    "post_body": post_body if not None else post_html,
                }
                post: BeautifulSoup
                post, post_data = self.build_post_html(
                    post_data, post_elements, post_template
                )

                # 3. Get Avatars from Post Body
                avatars = yield from self.handle_avatars_covers(post, post_data)

                # 4. Get Gallery Media
                media = self._media_from_post(post, post_data)
                images = yield from self.get_gallery_media(media, post_data)

                # 5 Update Gallery to local values
                if gallery := post.find("div", {"class": "uploads-images"}):
                    gallery["data-gallery"] = ""

                # 6. Remove all links
                for link in post.select("[src^='http']"):
                    link.src = ""

                # 6. Re-Link all local references back to the website
                for link in post.find_all("a"):
                    if link["href"].startswith("/"):
                        if self.post_config.get("relink"):
                            link["href"] = self.root + link["href"]
                        else:
                            link["href"] = None

                # 7. Create Vars File
                yield from self.build_html_vars_file(
                    post, post_data, images=images, avatars=avatars
                )

                # 7. Commit Post
                post_data = post_data.model_dump()
                post_data["extension"] = "html"
                post_data["type"] = "post"
                post_string = f"text:{str(post)}"
                yield Message.Url, post_string, post_data
            elif self.post_config.get("style") == "text":
                yield self._commit_journal_text(post_data)

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

        def check_errors(_response):
            errors: dict
            if errors := _response.json().get("errors"):
                self.log.debug(errors)
                try:
                    msg = f'"{errors.popitem()[1]}"'
                except (AttributeError, IndexError):
                    msg = None
                raise exception.AuthenticationError(msg)
            return _response

        # submit username / email
        url = self.root + "/session.json"
        data = {"email": username}
        check_errors(
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

    def handle_avatars_covers(
        self, post: Tag, post_data: PostData
    ) -> Generator[tuple[int, str, Any], Any, dict[Any, Any]]:
        avatars = {}
        supported_types = ["avatar", "cover"]
        for element in post.find_all(
            "img",
            {
                "data-type": supported_types,
                "src": True,
            },
        ):
            user_id = element.get("data-user-id")
            el_type = element.get("data-type")
            key = f"{user_id}_{el_type}"

            if not (username := element.get("alt")):
                if name_element := element.find_next(
                    "div",
                    class_=["profile_main_info-name", "post-user", "comments-author"],
                ):
                    username = name_element.text
                else:
                    username = None

            if avatars.get(key) is None and el_type in supported_types:
                if self.post_config.get("avatars") or (
                    self.post_config.get("cover") and el_type == "cover"
                ):
                    avatar = AvatarItem(
                        **post_data.model_dump(),
                        user_id=int(user_id),
                        username=username,
                        type=el_type,
                        extension=text.ext_from_url(element["src"]),
                        url=element["src"],
                    )

                    avatar_data = avatar.model_dump()
                    yield Message.Url, avatar.url, avatar_data
                    avatar_item = {
                        "userId": int(user_id),
                        "username": username,
                        "filename": avatar_data["_gdl_path"].filename,
                        "type": el_type,
                    }
                else:
                    yield 0, "", None
                    avatar_item = None

                avatars[key] = avatar_item

            # Always set a source to nothing to make it offline
            element["src"] = ""

        return avatars

    def get_post_body(self, post: Tag, post_id: int) -> bool | Tag:
        post_body = self.soup.new_tag("div", attrs={"class": "section-body"})
        if body := post.find("div", class_="post-body"):
            post_body.append(body)
        if collections := post.find("div", class_="collection_labels"):
            # TODO - see about getting collection links
            collections.find("img")["src"] = ""
            post_body.append(collections)
        if tags := post.find("div", class_=["tags", "post_tags"]):
            post_body.append(tags)

        # TODO - Validate if we have to do this everytime or not
        # TODO - Also validate what happens if there are a lot of comments
        if self.post_config.get("comments"):
            actions = post.find("div", class_="post-actions")
            container = actions.find("div", attrs={"data-role": "post-comments_list"})
            comments = self._get_post_comments(post_id)
            container.append(comments)
            post_body.append(actions)

        return post_body

    def get_gallery_media(self, media: list[GalleryItem], post_data: PostData):
        images = []
        for num, item in enumerate(media, 1):
            item.num = num

            if item.url.startswith("/"):
                item.url = self.root + item.url

            if item.original_filename:
                item.extension = item.original_filename.split(".")[-1]
                item.name = ".".join(item.original_filename.split(".")[:-1])

            # Download the normal image and link
            item_data = item.model_dump()
            yield Message.Directory, "", item.model_dump()
            yield Message.Url, item.url, item_data
            if isinstance(item, GalleryItem):
                images.append(
                    {
                        "id": item.id,
                        "filename": item_data["_gdl_path"].filename,
                        "created_at": item.date,
                        "width": item.width,
                        "height": item.height,
                        "type": item.type,
                    }
                )

            try:
                # Attempt to get the previews if the files are too large, or they are requested
                file_size = os.path.getsize(item_data["_gdl_path"].path) / 1024 / 1024
                file_limit = self.post_config.get("previews", False)
                if file_limit is not False and file_size > file_limit:
                    item.type = "preview"
                    yield Message.Directory, "", item.model_dump()
                    yield Message.Url, item.gallery_preview_url, item.model_dump()
                    post_data.previews = True

            except FileNotFoundError:
                pass  # If we don't download the main image, then we skip the preview
        return images

    @cache()
    def get_post_sidebar(self, post_id: int):
        if sidebar := self.soup.find("div", class_="for-single_post_sidebar"):
            return sidebar
        else:
            response = self.request(self.root + f"/{post_id}").text
            page = BeautifulSoup(response, "html.parser")
            sidebar = page.find("div", class_="for-single_post_sidebar")
            return sidebar

    @staticmethod
    def _media_from_post(
        post: Tag, post_data: PostData
    ) -> list[GalleryItem | AvatarItem]:
        # TODO - Need to work on making this more stable with the count
        media = {}
        if gallery := post.find("div", {"class": "uploads-images"}):
            gallery_data = util.json_loads(gallery["data-gallery"])
            post_data.gallery_count = len(gallery_data)
            for image in gallery_data:
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
                media[audio["id"]] = {
                    "id": text.parse_int(text.extr(audio, 'data-upload-id="', '"')),
                    "name": text.unescape(
                        text.extr(audio, 'audio_preview-title">', "<")
                    ),
                    "url": text.unescape(text.extr(audio, 'src="', '"')),
                    "type": "audio",
                }
        post_data.media_count = len(media.values())
        return list(media.values())

    def _data_from_post(self, html: Tag) -> PostData:
        if author_bio := html.find(
            "div", class_=["star_link-types", "profile_main_info-description"]
        ):
            author_bio = author_bio.text.strip()
        elif author_bio := self.soup.find(
            "div", class_="profile_main_info-description"
        ):
            author_bio = author_bio.text.strip()
        else:
            author_bio = ""

        title_element = html.find("div", class_=["trix-content", "post-title"])
        if post_title := title_element.find(["h1", "h2", "h3"]):
            post_title = post_title.text.strip()
        else:
            post_title = ""

        tags = []
        if tag_element := html.find("div", class_="post_tags"):
            for tag in tag_element.findChildren("a", recursive=False):
                tags.append(tag.text)

        if "post" in html.get("class") and html.get("data-id"):
            post_id = html["data-id"]
        elif post_container := html.find(
            "div", {"class": "section-body", "data-id": True}
        ):
            post_id = post_container["data-id"]
        else:
            post_id = None

        if raw_date := html.find(["div"], class_=["section-title_date", "post-date"]):
            date = self._parse_datetime(raw_date.text.strip())
        else:
            date = None

        if collection := html.find("div", class_="collection_label for-post"):
            link = collection.find("a", attrs={"href": True})["href"]
            collection_id = link.split("/")[-1]
            collection_name = collection.find(
                "span", class_="collection_label-title"
            ).text.strip()
        else:
            collection_id = None
            collection_name = None

        return PostData(
            author=html.find(
                ["a", "div"],
                class_=[
                    "star_link-name",
                    "post-user for-profile_wall",
                ],
            ).text.strip(),
            author_bio=author_bio,
            author_id=html.find(
                ["a", "div"], class_=["star_link-avatar", "post-avatar"]
            ).next.get("data-user-id"),
            collection_id=collection_id,
            collection_name=collection_name,
            date=date,
            title=post_title,
            post_id=post_id,
            tags=tags,
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

    def build_post_html(
        self, post_data: PostData, post_elements: dict[str, Tag], template: str
    ):
        if not post_elements["post_body"]:
            self.log.warning("%s: Empty post content", post_data.post_id)
            return None
        else:
            html_string = template.format(
                post_body=post_elements["post_body"],
                post_date=post_data.date,
                author=post_elements["author_element"] or "",
                post_title=post_data.title,
                post_id=post_data.post_id,
            )
            html = BeautifulSoup(html_string, "html.parser")
            return html, post_data

    def build_html_vars_file(
        self,
        post: Tag,
        post_data: PostData,
        images: list[GalleryItem],
        avatars: dict[str, str],
    ):
        data = post_data.model_dump()

        # Get Relative Paths
        data["type"] = "avatar"
        data["extension"] = "png"
        yield Message.Directory, "", data
        avatar_path = Path(data["_gdl_path"].directory)

        data["type"] = "cover"
        yield Message.Directory, "", data
        cover_path = Path(data["_gdl_path"].directory)

        data["type"] = "image"
        yield Message.Directory, "", data
        image_path = Path(data["_gdl_path"].directory)

        _preview: bool | int = self.post_config.get("previews")
        if post_data.previews:
            data["type"] = "preview"
            yield Message.Directory, "", data
            preview_path = Path(data["_gdl_path"].directory)
        else:
            preview_path = image_path

        # Webpage Files
        data["extension"] = "html"
        data["type"] = "post"
        yield Message.Directory, "", data
        post_path = Path(data["_gdl_path"].directory)

        data["extension"] = "js"
        data["type"] = "vars"
        yield Message.Directory, "", data
        vars_path = Path(data["_gdl_path"].directory)

        var_file = HTML_VAR_TEMPLATE.format(
            post_id=post_data.post_id,
            avatar_path=avatar_path.relative_to(post_path, walk_up=True),
            cover_path=cover_path.relative_to(post_path, walk_up=True),
            image_path=image_path.relative_to(post_path, walk_up=True),
            preview_path=preview_path.relative_to(post_path, walk_up=True),
            vars_path=vars_path.relative_to(post_path, walk_up=True),
            images=util.json_dumps(images),
            avatars=util.json_dumps(avatars),
        )
        yield Message.Url, var_file, data

        vars_container = post.find("script", class_="post_vars")
        vars_path = vars_path.relative_to(post_path, walk_up=True)
        vars_container["src"] = f"{vars_path}/{data['_gdl_path'].filename}"

        return data["_gdl_path"].filename

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
    def _get_profile_header(self, author: str):
        if header := self.soup.find("div", class_="profile_main_info"):
            return header
        else:
            response = self.request(self.root + f"/{author}").text
            page = BeautifulSoup(response, "html.parser")
            header = page.find("div", class_="profile_main_info")
            return header


class SubscribeStarUserExtractor(SubscribeStarExtractor):
    """Extractor for media from a subscribestar user"""

    subcategory = "user"
    pattern = (
        BASE_PATTERN + r"/(?!posts|collections)(?P<user>[^/?#]+)(\?tag=[\w]+)?(?:$|/$)"
    )
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
            else:
                break


class SubscribeStarCollectionsExtractor(SubscribeStarExtractor):
    """Extractor for media from a subscribestar user"""

    subcategory = "user"
    pattern = (
        BASE_PATTERN + r"/(?!posts)(?P<user>[^/?#]+)/collections/?"
        r"(?P<collection_id>\d+)?"
    )
    example = "https://www.subscribestar.com/USER/tag=TAGNAME"

    def __init__(self, match):
        super().__init__(match)
        self.collection_id: int = match.groupdict().get("collection_id")
        self.username: str = match.group("user")

    def posts(self) -> Generator[Tag, Any, None]:
        if self.collection_id is None:
            # Get Collection IDs
            page = self.request(self.url).text
            self.soup = BeautifulSoup(page, "html.parser")
            elements = self.soup.find_all("div", class_="post_collection")
            collection_ids = [
                int(element["data-collection-id"]) for element in elements
            ]
        else:
            collection_ids = [self.collection_id]

        for collection_id in collection_ids:
            url = f"{self.root}/{self.username}/collections/{collection_id}"
            page = self.request(url).text
            while True:
                self.soup = BeautifulSoup(page, "html.parser")
                if posts := self.soup.find_all("div", class_=["collection_post"]):
                    for post in posts:
                        post_url = f"{self.root}/posts/{post['data-post-id']}"
                        response = self.request(post_url)
                        post_soup = BeautifulSoup(response.text, "html.parser")
                        yield from post_soup.find_all("div", class_=["post", "wrapper"])
                else:
                    break

                url = self.soup.find("div", {"class": "posts-more"})
                if not url:
                    break
                page = self.request(self.root + text.unescape(url["href"])).json()[
                    "html"
                ]
            # while True:
            #     self.soup = BeautifulSoup(page, "html.parser")
            #     if posts := self.soup.find_all("div", class_="collection_post"):
            #         yield from posts
            #
            #         url = self.soup.find("div", {"class": "posts-more"})
            #         if not url:
            #             return
            #         page = self.request(self.root + text.unescape(url["href"])).json()[
            #             "html"
            #         ]
            #     else:
            #         break


class SubscribeStarPostExtractor(SubscribeStarExtractor):
    """Extractor for media from a single SubscribeStar post"""

    subcategory = "post"
    pattern = BASE_PATTERN + r"/posts/(?P<post_id>\d+)"
    example = "https://www.subscribestar.com/posts/12345"

    def __init__(self, match):
        super().__init__(match)
        self.post_id = match.group("post_id")

    def posts(self) -> ResultSet[Tag]:
        response = self.request(self.url)
        self.soup = BeautifulSoup(response.text, "html.parser")

        return self.soup.find_all("div", class_=["post", "wrapper"])


# region Templates
JOURNAL_TEMPLATE_TEXT = """text:{title}
by {username}, {date}

{content}
"""

JOURNAL_HEADER_TEMPLATE_HTML = """
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
    <script class="post_vars" src=""></script>
</head>
<body>
<div class="layout for-public is-adult" data-role="popup_anchor" data-view="app#layout" id="root">
    <div class="layout-inner" data-view="app#fix_scroll">
        <div class="layout-content">
            {author}
            <div class="post wrapper for-profile_columns is-single is-shown" data-comments-loaded="true"
                 data-view="app#post">
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
<script src="../../CSS/Gallery.js"></script>
</html>
"""

JOURNAL_SIDEBAR_TEMPLATE_HTML = """
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
    <script class="post_vars" src=""></script>
</head>
<body>
<div class="layout for-public is-adult" data-role="popup_anchor" data-view="app#layout" id="root">
    <div class="layout-inner" data-view="app#fix_scroll">
        <div class="layout-content">
            <div class="post wrapper for-profile_columns is-single is-shown" data-comments-loaded="true"
                 data-view="app#post">
                <div class="section for-single_post">
                    <div class="section-title">
                        <div class="section-title_date">{post_date}</div>
                    </div>
                    {post_body}
                </div>
                {author}
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
const avatars = {avatars}
const images = {images}
"""

# endregion Templates

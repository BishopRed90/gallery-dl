# -*- coding: utf-8 -*-

# Copyright 2020-2023 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Extractors for https://www.subscribestar.com/"""
from abc import abstractmethod

from .common import Extractor, Message
from bs4 import BeautifulSoup, ResultSet, Tag
from .. import text, util, exception
from ..cache import cache

BASE_PATTERN = r"(?:https?://)?(?:www\.)?subscribestar\.(com|adult)"


class SubscribeStarExtractor(Extractor):
    """Base class for SubscribeStar extractors"""
    category: str = "subscribestar"
    root: str = "https://www.subscribestar.com"
    directory_fmt: tuple = ("{category}", "{author_name}")
    filename_fmt: str = "{post_id}_{id}.{extension}"
    archive_fmt: str= "{id}"
    cookies_domain: str = ".subscribestar.com"
    cookies_names: str = ("_personalization_id",)
    _warning: bool = True

    def __init__(self, match):
        tld, self.item = match.groups()
        if tld == "adult":
            self.root = "https://subscribestar.adult"
            self.cookies_domain = ".subscribestar.adult"
            self.subcategory += "-adult"
            self.soup = BeautifulSoup("", "html.parser")

        Extractor.__init__(self, match)

    def items(self):
        self.login()
        for post_html in self.posts():
            media = self._media_from_post(post_html)
            data = self._data_from_post(post_html)
            # if _ := self.config('avatars', None):
            #     if avatar := self._avatar_from_post(data["author_id"] if _ == "author" else None):
            #         media += avatar

            data["title"] = text.unescape(text.extr(
                data["content"], "<h1>", "</h1>"))
            data["count"] = len(media)
            yield Message.Directory, data

            _re_link_cache = {}
            for num, item in enumerate(media, 1):
                item.update(data)
                if not item.get("type") == "avatar":
                    item["num"] = num
                text.nameext_from_url(item.get("name") or item["url"], item)
                if item["original_filename"] and not item["extension"]:
                    item["extension"] = item["original_filename"].split(".")[-1]
                    item["original_filename"] = ".".join(item["original_filename"].split(".")[:-1])
                if item["url"][0] == "/":
                    item["url"] = self.root + item["url"]
                yield Message.Url, item["url"], item

                if item.get('gallery_preview_url'):
                    _re_link_cache[item["gallery_preview_url"]] = item["_gdl_path"].path
                if item.get('preview_url'):
                    _re_link_cache[item["preview_url"]] = item["_gdl_path"].path
                if item.get('url'):
                    _re_link_cache[item["url"]] = item["_gdl_path"].path

                item['url'] = item['gallery_preview_url'] = item['preview_url'] = item["_gdl_path"].path

            for link in post_html.find_all('img'):
                link['src'] = _re_link_cache.get(link['src'], '')
                if not link['src']:
                    link['alt'] = ''
                else:
                    link['onclick'] = "window.open(this.src)"

            if gallery := post_html.find('div', {"class": "uploads-images"}):
                gallery['data-gallery'] = util.json_dumps(media)
                self._render_previews(post_html)

            journals = self.config("posts", "html")
            if journals == "text":
                yield self._commit_journal_text(data)
            elif journals == "html":
                for link in post_html.find_all('a'):
                    link['href'] = self.root + link['href']

                html_content = post_html.find('div', {'class': 'section for-single_post'})
                html_author = post_html.find('div', {'class': 'section for-single_post_sidebar is-sticky'})
                post_info = {
                    "html_author": str(html_author if None else ""),
                    "html_content": str(html_content if None else post_html),
                }
                if post_info["html_content"]:
                    yield self._commit_post_html(data, post_info)
                else:
                    yield self._commit_journal_text(data)

    @abstractmethod
    def posts(self):
        """Yield HTML content of all relevant posts"""

    def request(self, url, **kwargs):
        while True:
            response = Extractor.request(self, url, **kwargs)

            if response.history and (
                    "/verify_subscriber" in response.url or
                    "/age_confirmation_warning" in response.url):
                raise exception.StopExtraction(
                    "HTTP redirect to %s", response.url)

            content = response.content
            if len(content) < 250 and b">redirected<" in content:
                url = text.unescape(text.extr(
                    content, b'href="', b'"').decode())
                self.log.debug("HTML redirect message for %s", url)
                continue

            return response

    def login(self):
        if self.cookies_check(self.cookies_names):
            return

        username, password = self._get_auth_info()
        if username:
            self.cookies_update(self._login_impl(
                (username, self.cookies_domain), password))

        if self._warning:
            if not username or not self.cookies_check(self.cookies_names):
                self.log.warning("no '_personalization_id' cookie set")
            SubscribeStarExtractor._warning = False

    @cache(maxage=28*86400, keyarg=1)
    def _login_impl(self, username, password):
        username = username[0]
        self.log.info("Logging in as %s", username)

        if self.root.endswith(".adult"):
            self.cookies.set("18_plus_agreement_generic", "true",
                             domain=self.cookies_domain)

        # load login page
        url = self.root + "/login"
        page = self.request(url).text

        headers = {
            "Accept": "*/*;q=0.5, text/javascript, application/javascript, "
                      "application/ecmascript, application/x-ecmascript",
            "Referer": self.root + "/login",
            "X-CSRF-Token": text.unescape(text.extr(
                page, '<meta name="csrf-token" content="', '"')),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }

        def check_errors(_response):
            errors = _response.json().get("errors")
            if errors:
                self.log.debug(errors)
                try:
                    msg = '"{}"'.format(errors.popitem()[1])
                except Exception:
                    msg = None
                raise exception.AuthenticationError(msg)
            return response

        # submit username / email
        url = self.root + "/session.json"
        data = {"email": username}
        response = check_errors(self.request(
            url, method="POST", headers=headers, data=data, fatal=False))

        # submit password
        url = self.root + "/session/password.json"
        data = {"password": password}
        response = check_errors(self.request(
            url, method="POST", headers=headers, data=data, fatal=False))

        # return cookies
        return {
            cookie.name: cookie.value
            for cookie in response.cookies
        }

    @staticmethod
    def _media_from_post(html) -> list[dict]:
        media = {}
        if gallery := html.find('div', {"class": "uploads-images"}):
            for image in util.json_loads(gallery['data-gallery']):
                media[image['id']] = image

        if attachments := html.find_all('figure', class_='attachment'):
            for attachment in attachments:
                info = util.json_loads(attachment['data-trix-attachment'])
                info['url'] = attachment.find('img')['src']
                info['type'] = "attachment"
                info['id'] = info['uploadId']
                if not info.get('original_filename'):
                    info['original_filename'] = text.filename_from_url(info['url'])

                # Attachments for photos could not be in the image gallery
                # if so we need to download it
                if not media.get(info['uploadId']):
                    media[info['uploadId']] = info

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

    def _data_from_post(self, html):
        extr = text.extract_from(html)
        author_name = html.find(['a','div'], ['profile_main_info-name', 'star_link-name', 'post-user']).text.strip()
        author_id = html.find(['a', 'div'], ['star_link-avatar', 'post-avatar']).next.get('data-user-id')
        return {
            "post_id"    : html.select('[data-post_id]')[0]['data-post_id'],
            "author_id"  : author_id,
            "author_name": author_name,
            "date"       : self._parse_datetime(
                html.find('div', ['post-date', 'section-title_date']).text.strip()
            ),
            "content"    : extr('<body>', '</body>').strip()
        }

    def _avatar_from_post(self, user_id: int = None) -> list[dict]:
        avatars = []
        for avatar in self.soup.find_all('img', { 'alt': True, 'data-user-id': True if user_id is None else str(user_id)}):
            avatars.append(
                {
                    'url': avatar['src'],
                    'original_filename': f'avatar_{avatar["alt"]}',
                    'type': 'avatar'
                }
            )
        return avatars

    def _replace_links(self, post_html, links):
        pass

    def _render_previews(self, post_html):
        """ Add previews to the HTML that we are going to be rendering for the journal """
        if gallery := post_html.find('div', {"class": "uploads-images"}):
            preview_section = self.soup.new_tag('div', attrs={'class': 'previews', 'data-role': 'uploads_gallery-previews'})
            gallery.append(preview_section)
            previews = util.json_loads(gallery['data-gallery'])
            height = 100/min(len(previews), 1)
            width = 100/min(len(previews), 1)
            for preview in previews:
                if preview.get('type') == 'image':
                    preview_item = self.soup.new_tag('div', attrs={'class': 'preview', 'data-role':'uploads_gallery-preview upload', 'data-upload-id': preview['id'], 'style': f'width: {width}%; padding-bottom: {height}%;', 'onclick': "window.open(this.querySelector('img').src)"})
                    preview_inner= self.soup.new_tag('div', attrs={'class': 'preview-inner', 'data-role':'uploads_gallery-preview_inner'})
                    preview_img = self.soup.new_tag('img', attrs={'src': f'{preview['url']}', 'class': 'preview-img'})

                    preview_inner.append(preview_img)
                    preview_item.append(preview_inner)
                    preview_section.append(preview_item)

    @staticmethod
    def _parse_datetime(dt):
        if dt.startswith("Updated on "):
            dt = dt[11:]
        date = text.parse_datetime(dt, "%b %d, %Y %I:%M %p")
        if date is dt:
            date = text.parse_datetime(dt, "%B %d, %Y %I:%M %p")
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
                text.remove_html(txt)
                for txt in content.split("</div>")
            )
        txt = JOURNAL_TEMPLATE_TEXT.format(
            title=post["title"],
            username=post["author_nick"],
            date=post["date"],
            content=content,
        )

        post["extension"] = "txt"
        return Message.Url, txt, post

    def _commit_post_html(self, post, html_info):
        content = html_info["html_content"]
        author = html_info["html_author"]

        if not content:
            self.log.warning("%s: Empty post content", post["index"])
            return None
        else:
            html = JOURNAL_TEMPLATE_HTML.format(content=content, author=author)
            post["extension"] = "html"
            post["type"] = "post"
            return Message.Url, html, post

class SubscribeStarUserExtractor(SubscribeStarExtractor):
    """Extractor for media from a subscribestar user"""
    subcategory = "user"
    pattern = BASE_PATTERN + r"/(?!posts/)([^/?#]+)"
    example = "https://www.subscribestar.com/USER"

    def posts(self):
        page = self.request("{}/{}".format(self.root, self.item)).text

        while True:
            self.soup = BeautifulSoup(page, "html.parser")
            posts = self.soup.find_all('div', class_='post')

            if not posts:
                return
            yield from posts

            url = self.soup.find('div', {'class': 'posts-more'})
            if not url:
                return
            page = self.request(self.root + text.unescape(url['href'])).json()["html"]

class SubscribeStarPostExtractor(SubscribeStarExtractor):
    """Extractor for media from a single SubscribeStar post"""
    subcategory = "post"
    pattern = BASE_PATTERN + r"/posts/(\d+)"
    example = "https://www.subscribestar.com/posts/12345"

    def posts(self) -> ResultSet[Tag]:
        url = f"{self.root}/posts/{self.item}"
        response = self.request(url)
        self.soup = BeautifulSoup(response.text, "html.parser")

        return self.soup.select('div.post.wrapper')



JOURNAL_TEMPLATE_TEXT = """text:{title}
by {username}, {date}

{content}
"""

# TODO - Snatch the CSS File
JOURNAL_TEMPLATE_HTML = """text:<!DOCTYPE html>
<html lang="">
<head>
    <title>SubscribeStar.adult</title>
    <meta content="width=device-width, initial-scale=1, maximum-scale=1.0, user-scalable=no" name="viewport"/>
    <meta name="action-cable-url" content="/cable"/>
    <meta content="72c35a3ce4ae8fc1d49ae85a68cb22d5" name="p:domain_verify"/>
    <link rel="stylesheet" media="screen"
          href="SubscribeStar.css"
          data-track-change="true"/>
</head>
<body>
<div class="layout for-public is-adult" data-role="popup_anchor" data-view="app#layout" id="root">
    <div class="layout-inner" data-view="app#fix_scroll">
        <div class="layout-content">
            <div class="post wrapper for-profile_columns is-single is-shown" data-comments-loaded="true"
                 data-edit-path="/posts/1817339/edit?single_post_view=true" data-id="1817339" data-role="popup_anchor"
                 data-view="app#post">
                {content}
                {author}
            </div>
        </div>
    </div>
</div>
</body>
</html>
"""

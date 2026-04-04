import json
import re
from dataclasses import asdict, dataclass

from bs4 import BeautifulSoup, SoupStrainer, Tag

from gallery_dl import text
from gallery_dl.extractor.common import Extractor
from gallery_dl.extractor.message import Message

BASE_PATTERN = r"(?:https?://megasite.meanworld.com)/"
SUB_SITES = [
    "mean bitches",
    "mean amazon bitches",
    "slave orders",
    "mean dungeon",
    "deviant david",
    "megasite",
]
EXTRA_WORDS = ["movies"]


@dataclass
class SceneMeta:
    actors: list[str]
    clip_count: int
    clips: list[Tag]
    content: str
    date: str
    photo_count: int
    runtime: str
    scene: str
    sub_brand: str
    tags: list[str] = None

    extension: str = None
    num: int = 1
    type: str = None
    count: int = 1


class MeanBitchesExtractor(Extractor):
    category = "meanbitches"
    root = "https://megasite.meanworld.com"
    cookies_domain = ".megasite.meanworld.com"
    directory_fmt = ("{category}", "{creator[full_name]}")
    filename_fmt = "{id}_{title}_{num:>02}.{extension}"
    archive_fmt = "{id}_{scene}_{type}_{num}"

    def __init__(self, match):
        Extractor.__init__(self, match)
        self.scene = None

    def items(self):
        for scene in self.scenes():
            if scene.find("div", class_="buy_button") or not scene.find_all(
                "li", class_="dlicon"
            ):
                self.log.debug("Skipping - scene is not unlocked")
                continue

            scene_meta = asdict(self.scene_info(scene))
            download_button = scene.find_all("li", class_="dlicon")[-1]
            download_path = download_button.find("a", class_="border_btn").attrs["href"]
            scene_meta["id"] = download_path.split("upload/")[1].split("_")[0]
            download_url = self.root + download_path

            scene_meta["type"] = "video"
            scene_meta["extension"] = text.ext_from_url(download_url)

            yield Message.Directory, "", scene_meta
            yield Message.Url, download_url, scene_meta

            if self.config("pictures", False):
                if zip_nav := scene.find("ul", id="nav_zip_menu"):
                    zip_dl = zip_nav.find_all("a")[-1].attrs["href"]
                    zip_url = self.root + zip_dl
                    scene_meta["extension"] = text.ext_from_url(zip_url)
                    scene_meta["type"] = "zip"
                    yield Message.Url, zip_url, scene_meta
                elif nave_bar := scene.find("div", class_="vidImgButtons"):
                    scene_meta["type"] = "photo"
                    if pic_nav := nave_bar.find(
                        "a", class_="border_btn", recursive=False
                    ):
                        for pos, photo_path in enumerate(
                            self._get_photos(pic_nav.attrs["href"])
                        ):
                            photo_url = self.root + photo_path
                            scene_meta["extension"] = text.ext_from_url(photo_url)
                            scene_meta["num"] = pos
                            yield Message.Url, photo_url, scene_meta

            if self.config("clips", True):
                scene_meta["type"] = "clip"
                for pos, clip in enumerate(scene_meta["clips"]):
                    clip_url = self.root + clip

                    scene_meta["num"] = pos + 1
                    scene_meta["count"] = scene_meta["clip_count"]
                    scene_meta["extension"] = text.ext_from_url(clip_url)

                    yield Message.Url, clip_url, scene_meta

    def scenes(self) -> list[Tag]:
        """Return all page objects"""

    def scene_info(self, scene: Tag) -> SceneMeta:
        scene_info = scene.find("div", class_="gallery_info")

        if actor_info := scene_info.find("p", class_="link_light"):
            actors_elements = actor_info.find_all("a", class_="link_bright infolink")
            actors = [element.text for element in actors_elements]
        else:
            actors = None

        if title_parts := list(
            map(
                str.strip,
                scene.find("meta", attrs={"property": "og:title"})
                .attrs["content"]
                .split("-"),
            )
        ):
            strip_words = SUB_SITES + EXTRA_WORDS
            title = " ".join(
                [
                    part
                    for part in title_parts
                    if not any(
                        word.lower() in part.lower() for word in actors + strip_words
                    )
                ]
            ).strip()

            if not title or title.isdigit():
                # Older clips have just the name of the actress + year - so we will just use that
                title = " ".join(
                    [
                        part
                        for part in title_parts
                        if not any(word.lower() in part.lower() for word in strip_words)
                    ]
                ).strip()

        if sub_brand_ele := scene_info.find("a", class_="link_bright", recursive=False):
            sub_brand = sub_brand_ele.text
        else:
            sub_brand = None

        if video_info := scene_info.find("ul", class_="videoInfo"):
            elements = video_info.find_all("li")
            date = elements[0].text.strip()
            photo_count = elements[1].text.strip()
            runtime = elements[2].text.strip()
        else:
            date = None
            photo_count = None
            runtime = None

        if tag_element := scene_info.find("div", class_="blogTags"):
            tags = [tag.text for tag in tag_element.find("ul")]
        else:
            tags = None

        if clips_element := scene.find("div", class_="photosArea"):
            clips = clips_element.find_all("div", class_="latestUpdateB")
            clips = [
                clip.find("a", attrs={"onclick": True}).attrs["href"] for clip in clips
            ]
            clip_count = len(clips)

        else:
            clip_count = 0
            clips = []

        if scene_desc := scene_info.find("div", class_="vidImgContent text_light"):
            content = "\n".join([para.text for para in scene_desc.find_all("p")])
        else:
            content = None

        return SceneMeta(
            scene=title,
            actors=actors,
            clip_count=clip_count,
            clips=clips,
            sub_brand=sub_brand,
            date=date,
            photo_count=int(photo_count),
            runtime=runtime,
            content=content,
            tags=tags,
        )

    def _get_photos(self, uri: str):
        response = self.request(uri)
        strainer = SoupStrainer("script")
        soup = BeautifulSoup(response.text, "html.parser", parse_only=strainer)
        text_filter = re.compile(r"{src: \"(?!.*/thumbs).*};")
        images_raw = re.findall(text_filter, soup.decode())
        image_sources = {}
        for image_json in images_raw:
            # Sometimes there are higher and lower res values in the list
            # We overwright it with the higher resolution photo
            image_source = (
                re.search(r"(?:src:.+?)(?P<content>\".+?\")", image_json)
                .group("content")
                .strip('"')
            )
            file_name = image_source.split("/")[-1]

            image_sources[file_name] = image_source
        yield from image_sources.values()


class MeanBitchesSceneExtractor(MeanBitchesExtractor):
    subcategory = "scene"
    pattern = BASE_PATTERN + (r"scenes/(?P<scene>[\w-]+)")

    def __init__(self, match):
        MeanBitchesExtractor.__init__(self, match)
        self.scene = match.group("scene")
        self.soup = None

    def scenes(self):
        response = self.request(self.url)
        self.soup = BeautifulSoup(response.text, "html.parser")

        return [self.soup]

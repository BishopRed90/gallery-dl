from __future__ import annotations

from datetime import datetime
from enum import Enum, auto
from typing import Annotated, Any
from uuid import UUID

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    Field,
    PlainSerializer,
    computed_field,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)


### Supporting Types
class DeviationTypes(Enum):
    image = auto()
    film = auto()
    journal = auto()
    pdf = auto()


class Tag(BaseModel):
    name: str = Field(validation_alias=AliasChoices("name", "tag_name"))

    @model_serializer(mode="plain", return_type=str)
    def serialize(self):
        return self.name


class DeviationMedia(BaseModel):
    uri: str = Field(
        validation_alias=AliasChoices(
            "baseUri", "src", "url", AliasPath("media", "baseUri")
        )
    )
    prettyName: str = Field(
        validation_alias=AliasChoices("prettyName",
                                      AliasPath("media", "prettyName")),
        default=None
    )
    types: list[dict] = Field(
        validation_alias=AliasChoices("types", AliasPath("media", "types")),
        default=None,
        exclude=True,
    )

    token: list[str] | None = Field(
        validation_alias=AliasChoices("token", AliasPath("media", "token")),
        default=None,
    )
    height: int | None = Field(default=None)
    width: int | None = Field(default=None)
    type: str | None = None
    file_size: int | None = Field(default=None, alias="zipFilesize")
    src: str | None = Field(alias="url", default=None)
    transparency: bool | None = Field(default=None)
    extension: str | None = Field(default=None)
    position_: int = Field(default=0, alias="position", exclude=True)
    is_original: bool = Field(description="If the file resolution matches "
                                          "the maximum size", default=False)

    @computed_field
    @property
    def position(self) -> int:
        return self.position_ + 1

    @computed_field
    def media_view(self) -> dict | None:
        if self.types:
            videos = [video for video in self.types if
                      video.get("t") == "video"]
            if videos:
                video = max(videos, key=lambda x: x.get("h"))
                return video

        if self.types and self.token:
            if view := next(
                view for view in self.types if
                view.get("t") == "fullview"
            ):
                if view.get("c") is None:
                    self.is_original = True
                return view

    def model_post_init(self, context: Any, /) -> None:
        if self.media_view:
            self.height = self.media_view.get("h")
            self.width = self.media_view.get("w")
            self.type = self.media_view.get("t")
            if self.type == "video":
                self.src = self.media_view.get("b")
            elif path := self.media_view.get("c") and self.token:
                path = path.replace("<prettyName>", self.prettyName)
                self.src = f"{self.uri}{path}?token={self.token[0]}"
            else:
                self.src = f"{self.uri}?token={self.token[0]}"
                self.is_original = True

            self.file_size = self.media_view.get("f")

        if self.type == "video":
            # Video's have cover photos so we need to replace the URI for it
            # to be consistent
            self.uri = self.src


class AdditionalMedia(DeviationMedia):
    fileId: int
    filename: str
    additionalMedia: bool = True


class PremiumContent(DeviationMedia):
    # TODO - Figure out how to determine if they have purchased the content - general gallery doesn't allow it
    uri: str | None = Field(
        validation_alias=AliasChoices(
            "baseUri", "src", "url", AliasPath("media", "baseUri")
        ),
        default=None,
    )
    productid: int = Field(alias="subproductId")
    purchased: bool = Field(
        validation_alias=AliasChoices("hasUserPurchased",
                                      "has_user_purchased"),
        default=False,
    )
    assets: list[dict] = Field(default_factory=list)
    extension: str = "zip"


class GalleryFolder(BaseModel):
    id: int = Field(alias="folderId")
    uuid: UUID = Field(alias="gallectionUuid")
    parent_id: int | None = Field(alias="parentId")
    parent_name: str | None = Field(
        validation_alias=AliasChoices("parentName", "parent_name"),
        default=None
    )
    parent_uuid: Annotated[UUID, PlainSerializer(upper_uuid)] | None = Field(
        validation_alias=AliasChoices("parentUuid", "parent_uuid"),
        default=None
    )
    title: str = Field(alias="name")
    items: int = Field(alias="totalItemCount", default=0)
    subfolders: list[GalleryFolder] = Field(default=[])

    @computed_field
    @property
    def subfolder(self) -> bool:
        return True if self.parent_id else False

    @field_serializer("parent_uuid", mode="plain")
    def upper_uuid(self, value: UUID | None) -> str | None:
        if value:
            return str(value).upper()
        else:
            return None


#### Sub Classes
class DeviationPremiumFolder(BaseModel):
    type: str
    access: bool = Field(
        validation_alias=AliasChoices("hasAccess", "has_access"))
    id: int = Field(validation_alias=AliasChoices("galleryId", "gallery_id"))
    url: str = Field(
        validation_alias=AliasChoices("galleryUrl", "gallery_url"))
    name: str = Field(
        validation_alias=AliasChoices("galleryName", "gallery_name"))


class Adoptable(BaseModel):
    id: int = Field(
        validation_alias=AliasChoices("deviationId", "deviation_id"))
    url: str = Field(
        validation_alias=AliasChoices("assetSourceUrl", "assetSourceUrl"))
    is_owner: bool = Field(
        validation_alias=AliasChoices("isOwner", "is_owner"))
    is_creator: bool = Field(
        validation_alias=AliasChoices("isCreator", "is_creator"))


class DeviationStats(BaseModel):
    comments: int
    favorites: int = Field(alias="favourites")
    downloads: int | None = None
    views: int | None = None
    private_collected: int | None = None


class DeviationTier(BaseModel):
    id: int
    subscribed: bool
    can_subscribe: bool
    price_dollar: str


class DeviantAuthor(BaseModel):
    id: int | None = Field(alias="userId", default=None)
    uuid: UUID = Field(validation_alias=AliasChoices("useridUuid", "userid"))
    username: str
    icon: str = Field(validation_alias=AliasChoices("usericon", "user_icon"))
    type: str

    # Bool Checks
    # group: bool = Field(validation_alias=AliasChoices('isGroup','is_group'))
    subscribed: bool = Field(
        validation_alias=AliasChoices("isSubscribed", "is_subscribed"),
        default=False
    )
    watching: bool = Field(
        validation_alias=AliasChoices("isWatching", "is_watching"),
        default=False
    )


class DeviationComments(BaseModel):
    more: bool = Field(validation_alias=AliasChoices("hasMore", "has_more"))
    less: bool = Field(validation_alias=AliasChoices("hasLess", "has_less"))
    total: int
    next_offset: int | None = Field(alias="nextOffset")
    comments: list[dict] = Field(alias="thread")


class OriginalFileInfo(BaseModel):
    height: int
    width: int
    file_size: int = Field(alias="filesize")
    type: str


class Deviation(BaseModel):
    """Represents the most core information found for a deviation.
    Every Check here is EclipseAPI -> OAuth Api as it is more verbose.
    """

    # Core properties
    author: DeviantAuthor
    weekly_downloads: int | None = Field(
        validation_alias=AliasChoices(
            AliasPath("extended", "downloadRestrictions", "remaining")
        ),
        default=None
    )

    id: int | None = Field(
        alias="deviationId",
        default=None,
    )
    media: DeviationMedia | AdditionalMedia | None = Field(
        validation_alias=AliasChoices("media", "content")
    )
    published_time: datetime = Field(
        validation_alias=AliasChoices("published_time", "publishedTime")
    )
    uuid: Annotated[UUID, PlainSerializer(upper_uuid)] | None = Field(
        validation_alias=AliasChoices(
            AliasPath("extended", "deviationUuid"), "deviationid"
        ),
        default=None,
    )
    stats: DeviationStats
    title: str
    tier_access: str = Field(
        validation_alias=AliasChoices("tierAccess", "tier_access"),
        default="unlocked"
    )
    url: str

    # Core Booleans
    purchasable: bool = Field(
        validation_alias=AliasChoices("isPurchasable", "is_purchasable")
    )
    blocked: bool = Field(
        validation_alias=AliasChoices("isBlocked", "is_blocked"))
    commentable: bool = Field(
        validation_alias=AliasChoices("isCommentable", "allows_comments")
    )
    deleted: bool = Field(
        validation_alias=AliasChoices("isDeleted", "is_deleted"))
    downloadable: bool = Field(
        validation_alias=AliasChoices("isDownloadable", "is_downloadable")
    )
    favorited: bool = Field(
        validation_alias=AliasChoices("isFavourited", "is_favourited")
    )
    mature: bool = Field(
        validation_alias=AliasChoices("isMature", "is_mature"))
    published: bool = Field(
        validation_alias=AliasChoices("isPublished", "is_published")
    )

    # Extended Data
    adoptable: Adoptable | None = Field(
        validation_alias=AliasPath("extended", "adoptable"), default=None
    )
    description_text: str | None = Field(
        validation_alias=AliasChoices(
            AliasPath("extended", "descriptionText", "excerpt"), "description"
        ),
        default=None,
    )
    description_html: dict | None = Field(
        validation_alias=AliasPath("extended", "descriptionText", "html"),
        default=None,
    )
    tags: list[Tag] | list[str] = Field(
        validation_alias=AliasChoices(AliasPath("extended", "tags"), "tags"),
        default=[]
    )
    multiImage: bool = Field(alias="isMultiImage", default=False)
    download: DeviationMedia | None = Field(
        validation_alias=AliasPath("extended", "download"), default=None
    )
    additional_media: list[AdditionalMedia | PremiumContent] = Field(
        validation_alias=AliasChoices(
            AliasPath("extended", "additionalMedia"), "additional_media"
        ),
        default=[],
    )
    premium_folder: DeviationPremiumFolder | None = Field(
        validation_alias=AliasChoices("premiumFolderData",
                                      "premium_folder_data"),
        default=None,
    )
    premium_content: PremiumContent | None = Field(
        validation_alias=AliasChoices(AliasPath("extended", "pcp"), "pcp"),
        default=None
    )
    folder: GalleryFolder | None = Field(default=None)
    original_file: OriginalFileInfo | None = Field(
        validation_alias=AliasPath("extended",
                                   "originalFile"),
        default=None)

    @model_validator(mode="before")
    @classmethod
    def _journal(cls, data: Any) -> Any:
        if data.get("media").get("baseUri"):
            return data
        else:
            data["media"] = None
            return data

    @model_validator(mode="after")
    def _validate_extended(self):
        self.published_time = datetime.fromtimestamp(
            self.published_time.timestamp())

        if self.id is None:
            # TODO - This may need to be updated to make sure we have processed sta.sh and other values
            self.id = int(self.url.split("-")[-1])

        if self.media:
            if not self.media.extension:
                self.media.extension = self.media.uri.split(".")[-1]

            if self.media.token and self.media.file_size is None:
                self.media.file_size = next(
                    view.get("f")
                    for view in self.media.types
                    if view.get("t") == "fullview"
                )

            if self.original_file:
                if (self.media.height == self.original_file.height and
                    self.media.width == self.original_file.width):
                    self.media.is_original = True

        return self

    @computed_field
    @property
    def extension(self) -> str:
        return self.media.extension if self.media else ""

    @computed_field
    @property
    def count(self) -> int:
        return 1 + len(self.additional_media)

    @computed_field
    @property
    def num(self) -> int:
        return self.media.position if self.media else 0

    @field_serializer("uuid", mode="plain")
    def upper_uuid(self, value: UUID | None) -> str | None:
        if value:
            return str(value).upper()
        else:
            return None

    @field_validator("adoptable", mode="before")
    @classmethod
    def purchase_adoptable(cls, value: Any) -> Any | None:
        if value["isOwner"]:
            return value
        else:
            return None

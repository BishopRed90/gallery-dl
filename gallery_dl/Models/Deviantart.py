from __future__ import annotations
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, AliasChoices, AliasPath, \
    computed_field, model_validator, model_serializer
from enum import Enum, auto

### Supporting Types
class DeviationTypes(Enum):
    image = auto()
    film = auto()
    journal = auto()
    pdf = auto()

class Tag(BaseModel):
    name: str = Field(validation_alias=AliasChoices('name','tag_name'))

    @model_serializer(mode="plain", return_type=str)
    def serialize(self):
        return self.name

class DeviationMedia(BaseModel):
    uri: str = Field(validation_alias=AliasChoices('baseUri','src', 'url', AliasPath('media','baseUri')))
    token: list[str] | None = Field(validation_alias=AliasChoices('token', AliasPath('media', 'token')), default=None)
    prettyName: str | None = Field(validation_alias=AliasChoices('prettyName', AliasPath('media', 'prettyName')), default=None)
    types: list[dict] | None = Field(validation_alias=AliasChoices('types', AliasPath('media', 'types')), default=None, exclude=True)
    height: int | None = Field(default=None)
    width: int | None = Field(default=None)
    transparency: bool | None = Field(default=None)
    filesize: int | None = Field(default=None, alias="zipFilesize")
    extension: str | None = Field(default=None, alias="type")
    position: int = Field(default=0)

    @computed_field
    @property
    def src(self) -> str:
        if self.token and self.types:
            base_uri = self.uri
            token = "?token=" + self.token[0]
            view = next(view.get('c') for view in self.types if view.get('t') == "fullview")
            if view:
                view = view.replace("<prettyName>", self.prettyName)
                return base_uri + view + token
            else:
                return base_uri + token
        else:
            return self.uri

class AdditionalMedia(DeviationMedia):
    fileId: int
    position: int
    filename: str

class PremiumContent(DeviationMedia):
    productid: int = Field(alias="subproductId")
    purchased: bool = Field(validation_alias=AliasChoices('hasUserPurchased','has_user_purchased'))
    assets: list[dict]
    extension: str = "zip"

class GalleryFolder(BaseModel):
    id: int = Field(alias="folderId")
    uuid: UUID = Field(alias="gallectionUuid")
    parent_id: int | None = Field(alias='parentId')
    parent_name: str | None = Field(default = None)
    title: str = Field(alias="name")
    items: int = Field(alias="totalItemCount", default=0)
    subfolders: list[GalleryFolder] = Field(default=[])

    @computed_field
    @property
    def subfolder(self) -> bool:
        return True if self.parent_id else False

#### Sub Classes
class DeviationPremiumFolder(BaseModel):
    type: str
    access: bool = Field(validation_alias=AliasChoices('hasAccess','has_access'))
    id: int = Field(validation_alias=AliasChoices('galleryId','gallery_id'))
    url: str = Field(validation_alias=AliasChoices('galleryUrl','gallery_url'))
    name: str = Field(validation_alias=AliasChoices('galleryName','gallery_name'))

class Adoptable(BaseModel):
    id: int = Field(validation_alias=AliasChoices('deviationId','deviation_id'))
    url: str = Field(validation_alias=AliasChoices('assetSourceUrl','assetSourceUrl'))
    is_owner: bool = Field(validation_alias=AliasChoices('isCreator','is_creator'))
    is_creator: bool = Field(validation_alias=AliasChoices('isOwner','is_owner'))

class DeviationStats(BaseModel):
    comments: int
    favorites: int = Field(alias='favourites')
    downloads: int | None = None
    views: int | None = None
    private_collected: int | None = None

class DeviationTier(BaseModel):
    id: int
    subscribed: bool
    can_subscribe: bool
    price_dollar: str

class DeviantAuthor(BaseModel):
    id: int | None = Field(alias='userId', default=None)
    uuid: UUID = Field(validation_alias=AliasChoices('useridUuid','userid'))
    username: str
    icon: str = Field(validation_alias=AliasChoices('usericon','user_icon'))
    type: str

    # Bool Checks
    # group: bool = Field(validation_alias=AliasChoices('isGroup','is_group'))
    subscribed: bool = Field(validation_alias=AliasChoices('isSubscribed','is_subscribed'), default=False)
    watching: bool  = Field(validation_alias=AliasChoices('isWatching','is_watching'), default=False)

class DeviationComments(BaseModel):
    more: bool = Field(validation_alias=AliasChoices('hasMore','has_more'))
    less: bool = Field(validation_alias=AliasChoices('hasLess', 'has_less'))
    total: int
    next_offset: int | None = Field(alias="nextOffset")
    comments: list[dict] = Field(alias='thread')

class Deviation(BaseModel):
    """Represents the most core information found for a deviation.
       Every Check here is EclipseAPI -> OAuth Api as it is more verbose.
    """
    # Core properties
    author: DeviantAuthor
    id: int | None = Field(alias='deviationId', default=None)
    media: DeviationMedia = Field(validation_alias=AliasChoices('media','content'))
    published_time: datetime = Field(validation_alias=AliasChoices('published_time','publishedTime'))
    uuid: UUID | None = Field(validation_alias=AliasChoices(AliasPath('extended', 'deviationUuid'), 'deviationid'), default=None)
    stats: DeviationStats
    title: str
    tier_access: str = Field(validation_alias=AliasChoices('tierAccess','tier_access'), default="unlocked")
    url: str

    # Core Booleans
    purchasable: bool = Field(validation_alias=AliasChoices("isPurchasable", "is_purchasable"))
    blocked: bool = Field(validation_alias=AliasChoices('isBlocked','is_blocked'))
    commentable: bool = Field(validation_alias=AliasChoices('isCommentable','allows_comments'))
    deleted: bool = Field(validation_alias=AliasChoices('isDeleted','is_deleted'))
    downloadable: bool = Field(validation_alias=AliasChoices('isDownloadable','is_downloadable'))
    favorited: bool = Field(validation_alias=AliasChoices('isFavourited','is_favourited'))
    mature: bool = Field(validation_alias=AliasChoices('isMature','is_mature'))
    published: bool = Field(validation_alias=AliasChoices('isPublished','is_published'))

    # Extended Data
    adoptable: Adoptable | None = Field(validation_alias=AliasPath('extended', 'adoptable'), default=None)
    description: str | None = Field(validation_alias=AliasChoices(AliasPath('extended', 'descriptionText',"excerpt"), 'description'), default=None)
    tags: list[Tag] = Field(validation_alias=AliasChoices(AliasPath('extended','tags'), 'tags'), default=[])
    multiImage: bool = Field(alias='isMultiImage', default=False)
    download: DeviationMedia | None = Field(validation_alias=AliasPath('extended', 'download'), default=None)
    additional_media: list[AdditionalMedia] = Field(validation_alias=AliasChoices(AliasPath('extended','additionalMedia'), 'additional_media'), default=[])
    premium_folder: DeviationPremiumFolder | None = Field(validation_alias=AliasChoices("premiumFolderData",'premium_folder_data'), default=None)
    premium_content: PremiumContent | None = Field(validation_alias=AliasChoices(AliasPath('extended', 'pcp'),'pcp'), default=None)
    folder: GalleryFolder | None = Field(default=None)

    @model_validator(mode='after')
    def _validate_extended(self):
        self.published_time = datetime.fromtimestamp(self.published_time.timestamp())

        if self.id is None:
            # TODO - This may need to be updated to make sure we have processed sta.sh and other values
            self.id = int(self.url.split('-')[-1])

        if not self.media.extension:
            self.media.extension = self.media.uri.split('.')[-1]

        if self.media.token and self.media.filesize is None:
            self.media.filesize = next(view.get('f') for view in self.media.types if view.get('t') == "fullview")

        return self

    @computed_field
    @property
    def extension(self) -> str:
        return self.media.extension

    @computed_field
    @property
    def count(self) -> int:
        return 1 + len(self.additional_media)



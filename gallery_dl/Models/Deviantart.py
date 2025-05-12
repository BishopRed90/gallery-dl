from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, Field, AliasChoices, AliasPath, \
    computed_field, model_validator, model_serializer
from enum import Enum, auto

### Supporting Classes
class Tag(BaseModel):
    name: str = Field(validation_alias=AliasChoices('name','tag_name'))

    @model_serializer(mode="plain", return_type=str)
    def serialize(self):
        return self.name

class DeviationTypes(Enum):
    image = auto()
    film = auto()
    journal = auto()
    pdf = auto()

#### Sub Classes
class DeviationStats(BaseModel):
    comments: int
    favorites: int = Field(alias='favourites')
    downloads: int | None = None
    views: int | None = None
    private_collected: int | None = None

class DeviationPremiumFolder(BaseModel):
    type: str
    access: bool = Field(validation_alias=AliasChoices('hasAccess','has_access'))
    id: int = Field(validation_alias=AliasChoices('galleryId','gallery_id'))

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
    subscribed: bool = Field(validation_alias=AliasChoices('isSubscribed','is_subscribed'))
    watching: bool  = Field(validation_alias=AliasChoices('isWatching','is_watching'))

class DeviationComments(BaseModel):
    more: bool = Field(validation_alias=AliasChoices('hasMore','has_more'))
    less: bool = Field(validation_alias=AliasChoices('hasLess', 'has_less'))
    total: int
    next_offset: int | None = Field(alias="nextOffset")
    comments: list[dict] = Field(alias='thread')

class DeviationMedia(BaseModel):
    uri: str = Field(validation_alias=AliasChoices('baseUri','src'))
    token: list[str] | None = Field(default=None)
    prettyName: str | None = Field(default=None)
    types: list[dict] | None = Field(default=None, exclude=True)
    height: int | None = Field(default=None)
    width: int | None = Field(default=None)
    transparency: bool | None = Field(default=None)
    filesize: int | None = Field(default=None)

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

class Deviation(BaseModel):
    """Represents the most core information found for a deviation.
       Every Check here is EclipseAPI -> OAuth Api as it is more verbose.
    """
    # Core properties
    author: DeviantAuthor
    id: int | None = Field(alias='deviationId', default=None)
    media: DeviationMedia = Field(validation_alias=AliasChoices('media','content'))
    published_time: datetime = Field(validation_alias=AliasChoices('published_time','publishedTime'))
    uuid: UUID = Field(validation_alias=AliasChoices(AliasPath('extended', 'deviationUuid'), 'deviationid'))
    stats: DeviationStats
    title: str
    url: str

    # Core Booleans
    blocked: bool = Field(validation_alias=AliasChoices('isBlocked','is_blocked'))
    commentable: bool = Field(validation_alias=AliasChoices('isCommentable','allows_comments'))
    deleted: bool = Field(validation_alias=AliasChoices('isDeleted','is_deleted'))
    downloadable: bool = Field(validation_alias=AliasChoices('isDownloadable','is_downloadable'))
    favorited: bool = Field(validation_alias=AliasChoices('isFavourited','is_favourited'))
    mature: bool = Field(validation_alias=AliasChoices('isMature','is_mature'))
    published: bool = Field(validation_alias=AliasChoices('isPublished','is_published'))

    @model_validator(mode='after')
    def _validate_extended(self):
        if self.id is None:
            self.id = int(self.url.split('-')[-1])
        self.published_time = datetime.fromtimestamp(self.published_time.timestamp())
        if self.media.token and self.media.filesize is None:
            self.media.filesize = next(view.get('f') for view in self.media.types if view.get('t') == "fullview")
        return self

class AdditionalMedia(BaseModel):
    media: DeviationMedia = Field(validation_alias=AliasChoices('media','additional_media'))

class ExtendedDeviation(Deviation):
    """Represents a single deviation with extended properties"""
    description: str | None = Field(validation_alias=AliasChoices(AliasPath('extended', 'descriptionText',"excerpt"), 'description'), default=None)
    tags: list[Tag] = Field(validation_alias=AliasChoices(AliasPath('extended','tags'), 'tags'))
    multiImage: bool | None = Field(alias='isMultiImage', default=None)
    additional_media: list[AdditionalMedia] | None = Field(validation_alias=AliasChoices(AliasPath('extended','additionalMedia'), 'additional_media'), default=None)

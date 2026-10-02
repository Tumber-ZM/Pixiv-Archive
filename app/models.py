from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Author:
    id: Optional[int] = None
    pixiv_user_id: int = 0
    name: str = ""
    profile_image: Optional[str] = None


@dataclass
class Artwork:
    id: Optional[int] = None
    pixiv_id: int = 0
    title: Optional[str] = None
    description: Optional[str] = None
    author_id: Optional[int] = None
    author_name: Optional[str] = None
    create_date: Optional[str] = None
    page_count: int = 1
    width: Optional[int] = None
    height: Optional[int] = None
    pixiv_status: str = "active"
    first_seen: Optional[str] = None
    last_synced: Optional[str] = None
    local_path: Optional[str] = None
    tags: list["Tag"] = field(default_factory=list)
    images: list["Image"] = field(default_factory=list)


@dataclass
class Tag:
    id: Optional[int] = None
    name: str = ""
    translated_name: Optional[str] = None


@dataclass
class Image:
    id: Optional[int] = None
    artwork_id: Optional[int] = None
    page: int = 0
    path: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    sha256: Optional[str] = None
    phash: Optional[str] = None
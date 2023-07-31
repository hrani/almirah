"""Model classes to represent layouts and its components."""

import os
import logging

from typing import List
from typing import Dict

from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy import tuple_
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import relationship
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import attribute_keyed_dict

from .db import Base, SessionManager
from .standard import Specification


class Tag(Base):
    """Generic tag representation."""

    __tablename__ = "tags"
    path: Mapped[str] = mapped_column(
        "path", ForeignKey("files.path"), primary_key=True
    )
    name: Mapped[str] = mapped_column("name", primary_key=True)
    value: Mapped[str] = mapped_column("value")

    file: Mapped["File"] = relationship(back_populates="tags")

    def __init__(self, path, name, value):
        self.path = path
        self.name = name
        self.value = value

    def __repr__(self):
        return f"<Tag {self.name}: '{self.value}'>"


class File(Base):
    """Generic file representation."""

    __tablename__ = "files"
    path: Mapped[str] = mapped_column("path", primary_key=True)
    root: Mapped[str] = mapped_column("root", ForeignKey("layouts.root"))

    layout: Mapped["Layout"] = relationship(back_populates="files")
    tags: Mapped[Dict[str, "Tag"]] = relationship(
        collection_class=attribute_keyed_dict("name"),
        cascade="all, delete-orphan",
    )

    def __init__(self, path, root):
        self.path = path
        self.root = root

    @property
    def rel_path(self):
        return os.path.relpath(self.path, self.root)

    def build_modified_path(self, changes):
        "Returns the path for file given changes to tags."
        t = {}
        for tag_name, tag in self.tags:
            t[tag_name] = tag.value
        t.update(changes)
        return self.layout.spec.build_path(t)

    def __repr__(self):
        return f"<File path={self.rel_path}>"


class Layout(Base):
    """Representation of file collection in a directory."""

    __tablename__ = "layouts"
    root: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]

    files: Mapped[List["File"]] = relationship(
        back_populates="layout", cascade="all, delete-orphan"
    )

    def __init__(
        self,
        root,
        name=None,
        spec=None,
        indexer=None,
        index=True,
    ):
        self.root = root
        self.name = name if name else os.path.basename(root)
        self.spec = spec if spec else Specification()
        self.indexer = indexer if indexer else Indexer()

        logging.info("Loading existing info if any on layout")
        existing_info = self.indexer._merge(self)
        self.files = existing_info.files
        self._sa_instance_state = existing_info._sa_instance_state

        if index:
            self.indexer(self)

    def get_files(self, **filters):
        """Return files that match criteria."""

        # Unpack filters dict to tuple
        tag_reqs = [(name, value) for name, value in filters.items()]

        # Construct table of passing file paths
        tag_filter = (
            select(Tag.path)
            .where(tuple_(Tag.name, Tag.value).in_(tag_reqs))
            .group_by(Tag.path)
            .having(func.count(Tag.name) == len(tag_reqs))
            .subquery()
        )

        # Build File objects from file paths
        file_filter = (
            select(File)
            .where(File.path.in_(select(tag_filter)))
            .where(File.root == self.root)
        )

        files = self.indexer.get(file_filter)
        return files

    def __repr__(self):
        return f"<Layout root='{self.root}'>"


class Indexer:
    """Index files in a Layout."""

    def __init__(self, session_manager=None):
        self.conn = session_manager
        if not session_manager:
            self.conn = SessionManager()

    def __call__(self, layout, only_valid=True):
        logging.info(f"Indexing layout with root {layout.root}")
        self.layout = layout
        self.only_valid = only_valid
        self._index_dir(self.layout.root)

    def _merge(self, obj):
        return self.conn.session.merge(obj)

    def _index_dir(self, dir):
        """Iteratively index all directories in layout."""
        logging.info(f"Indexing directory {dir}")
        SKIP_DIRS = ["sourcedata", "derivatives"]
        for content in os.listdir(dir):
            if content in SKIP_DIRS:
                logging.info(f"Skipping content {content}")
                continue

            path = os.path.join(dir, content)
            if os.path.isdir(path):
                self._index_dir(path)

            self._index_file(path)
        self.conn.session.commit()

    def _index_file(self, path):
        """Add valid files to persistent index."""
        logging.info(f"Indexing file at {path}")
        file = File(path, self.layout.root)
        if not self.layout.spec.validate(file.rel_path) and self.only_valid:
            logging.info("Skipping invalid file")
            return
        file = self._merge(file)
        if file in self.layout.files:
            logging.info("Skipping existing file")
            return
        self.layout.files.append(file)
        self._index_tags(file)
        self.conn.session.add(file)

    def _index_tags(self, file):
        """Add tags of file to persistent index."""
        logging.info("Adding file tags")
        tags = self.layout.spec.extract_tags(file.rel_path)
        for name, value in tags.items():
            tag = Tag(file.path, name, value)
            file.tags[tag.name] = tag
        file.tags["is_dir"] = Tag(file.path, "is_dir", os.path.isdir(file.path))

    def get(self, query):
        """Run a query on db associated and return all results."""
        res = self.conn.session.scalars(query).all()
        return res

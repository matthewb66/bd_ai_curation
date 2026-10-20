from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Origin(BaseModel):
    external_id: str = ""
    name: str = ""
    href: str = ""


class BomComponent(BaseModel):
    """A single BOM component entry fetched from GET {projver}/components (bill-of-materials-6)."""

    name: str
    version: str
    href: str
    component_id: str = ""
    component_version_id: str = ""
    match_types: list[str] = Field(default_factory=list)
    origins: list[Origin] = Field(default_factory=list)
    matched_file_paths: list[str] = Field(default_factory=list)
    # Paths as fetched from the server, before --filter_paths strips noise from matched_file_paths.
    all_matched_file_paths: list[str] = Field(default_factory=list)
    ignored: bool = False
    review_status: Optional[str] = None
    policy_status: Optional[str] = None
    # Only set for Match Review items (not yet in BOM); None for normal BOM components.
    match_review_type: Optional[str] = None
    input_external_ids: list[str] = Field(default_factory=list)
    usages: list[str] = Field(default_factory=list)
    # Matched-files link for entries without origins (unversioned binary matches).
    matched_files_href: str = ""

    def is_dependency(self) -> bool:
        dep_types = {'FILE_DEPENDENCY_DIRECT', 'FILE_DEPENDENCY_TRANSITIVE'}
        return any(m in dep_types for m in self.match_types)

    def is_signature(self) -> bool:
        sig_types = {'FILE_EXACT', 'FILE_SOME_FILES_MODIFIED',
                     'FILE_FILES_ADDED_DELETED_AND_MODIFIED', 'FILE_EXACT_FILE_MATCH'}
        return any(m in sig_types for m in self.match_types)

    def is_only_signature(self) -> bool:
        return self.is_signature() and not self.is_dependency()

    def is_reviewed(self) -> bool:
        return self.review_status == 'REVIEWED'


class SnippetBlock(BaseModel):
    """One unconfirmed snippet match: a fileSnippetBomComponents block plus the scanned
    file it belongs to. `source` keeps only the scan-file fields the confirm write needs
    (scanId, uri, compositePath); `block` is the raw Black Duck block.
    """

    hash_id: str
    component_name: str
    component_version: str
    component_href: str = ""
    scanned_path: str
    match_file_path: str = ""
    match_score: float
    match_coverage: int = 0
    matched_lines: int = 0
    # True when the scanned file's SHA-1 equals the matched upstream file's (byte-identical).
    exact_file: bool = False
    source: dict
    block: dict


class CurationAction(str, Enum):
    IGNORE = "IGNORE"
    CONFIRM_REVIEWED = "CONFIRM_REVIEWED"
    CONFIRM_MATCH_REVIEW = "CONFIRM_MATCH_REVIEW"
    CONFIRM_SNIPPET = "CONFIRM_SNIPPET"
    IGNORE_SNIPPET = "IGNORE_SNIPPET"
    REJECT_MATCH_REVIEW = "REJECT_MATCH_REVIEW"
    CONSOLIDATE = "CONSOLIDATE"
    NO_ACTION_MANUAL_REVIEW = "NO_ACTION_MANUAL_REVIEW"
    SET_VERSION = "SET_VERSION"


class CurationDecision(BaseModel):
    action: CurationAction
    target_name: str
    target_version: str
    target_href: str
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    match_review_type: Optional[str] = None
    input_external_id: Optional[str] = None
    # Only populated for CONSOLIDATE decisions.
    keep_href: Optional[str] = None
    ignore_hrefs: list[str] = Field(default_factory=list)
    # Only populated for SET_VERSION decisions: the KB version to apply, and the
    # usages carried over from the unversioned BOM entry.
    new_version_href: Optional[str] = None
    usages: list[str] = Field(default_factory=list)

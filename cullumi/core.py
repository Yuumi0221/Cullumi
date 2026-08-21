"""Compatibility exports for Cullumi's former monolithic core module."""

from .classification import (
    PHOTO_AI_FILTERS,
    PHOTO_ANALYSIS_COLUMNS,
    PHOTO_DECISION_FILTERS,
    PHOTO_UPSERT_SQL,
    classification_percentiles,
    classify,
    parse_photo_filter,
    photo_filter_where,
    photo_library_counts,
    project_photo_counts,
)
from .config import (
    APP_NAME,
    BUILTIN_PROFILES,
    ConfigStore,
    app_data_dir,
    validate_profile,
)
from .media import (
    DISPLAY_PREVIEW_EXTENSIONS,
    DISPLAY_PREVIEW_MAX_SIZE,
    HEIF_EXTENSIONS,
    IMAGE_EXTENSIONS,
    RAW_EXTENSIONS,
    VIDEO_EXTENSIONS,
    analyze_photo,
    display_preview_path,
    ensure_display_preview,
    open_heif,
    open_image,
)
from .project_store import (
    DATABASE_SCHEMA_VERSION,
    Project,
    ProjectManager,
    connect_db,
    project_id_for,
    project_thumbnail_path,
    project_thumbnail_storage_path,
    safe_relative_path,
)
from .scanner import DiscoveryResult, ScanCancelled, Scanner
from .similarity import (
    SimilarityGroupCache,
    build_similarity_groups,
    filename_sequence,
    hamming,
    hamming_candidate_pairs,
    image_structure,
    parse_taken,
    photo_shooting_key,
    quality_score,
)
from .workflows import (
    QUARANTINE_DIR,
    apply_quarantine,
    clear_decisions,
    export_decisions,
    import_decisions,
    mark_ai_remove_suggestions,
    quarantine_preview,
    restore_batch,
)

__all__ = [name for name in globals() if not name.startswith("_")]

"""Direct Copernicus Sentinel-2 L2A monthly vegetation composites.

This route is deliberately separate from native FORCE and swissEO. It reads
locally retrieved CDSE L2A bands plus SCL/CLD masks from DatasetCatalog,
creates a cloud-screened monthly reflectance median, then applies the shared
four source-neutral vegetation equations.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .catalog import SWITZERLAND_BBOX
from .dataset_catalog import DatasetCatalog
from .monthly import calendar_month_window
from .rolling import VEGETATION_L2A_ASSET_KEYS
from .vegetation import (
    DEFAULT_VEGETATION_NODATA,
    REFLECTANCE_BAND_NAMES,
    VEGETATION_INDEX_DEFINITIONS,
    VEGETATION_INDEX_NAMES,
    inspect_raster,
    locate_gdal,
    raster_extent,
    run_gdal,
    set_vrt_band_descriptions,
    write_described_cog,
    write_multiband_median,
    write_vegetation_indices,
)

COPERNICUS_REFLECTANCE_ASSETS: tuple[str, ...] = (
    "B03_10m",
    "B04_10m",
    "B05_20m",
    "B08_10m",
    "B8A_20m",
    "B11_20m",
)
COPERNICUS_QUALITY_ASSETS: tuple[str, ...] = ("SCL_20m", "CLD_20m")
DEFAULT_INVALID_SCL_CLASSES: tuple[int, ...] = (0, 1, 3, 8, 9, 10, 11)
COPERNICUS_MINIMUM_PHYSICAL_REFLECTANCE = 0.0
_VARIABLES = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_FLOAT32_BYTES = 4


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ValueError("bbox must contain west, south, east and north")
    west, south, east, north = (float(value) for value in bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox must be a valid WGS84 extent")
    return west, south, east, north


def _approximate_dimensions(bbox: Sequence[float], resolution: float) -> tuple[int, int]:
    west, south, east, north = bbox
    centre_latitude = (south + north) / 2
    width = (east - west) * 111_320.0 * math.cos(math.radians(centre_latitude))
    height = (north - south) * 111_320.0
    return math.ceil(width / resolution), math.ceil(height / resolution)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class CopernicusMonthlyScene:
    item_id: str
    tile_id: str
    acquisition_time: datetime
    cloud_cover: float | None
    assets: Mapping[str, dict[str, Any]]


def select_copernicus_scenes(
    pieces: Sequence[dict[str, Any]],
    *,
    max_cloud_cover: float | None,
) -> tuple[dict[str, list[CopernicusMonthlyScene]], dict[str, tuple[str, ...]]]:
    """Select complete L2A items and group them without mixing acquisition bands."""

    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for piece in pieces:
        grouped.setdefault(str(piece["item_id"]), {})[str(piece["asset_key"])] = piece
    required = set(VEGETATION_L2A_ASSET_KEYS)
    selected: dict[str, list[CopernicusMonthlyScene]] = {}
    skipped: dict[str, tuple[str, ...]] = {}
    for item_id, assets in grouped.items():
        reasons: list[str] = []
        missing = sorted(required.difference(assets))
        reasons.extend(f"missing:{key}" for key in missing)
        reasons.extend(
            f"missing-file:{key}"
            for key in sorted(required.intersection(assets))
            if not Path(str(assets[key]["local_path"])).is_file()
        )
        exemplar = next(iter(assets.values()))
        cloud_raw = exemplar.get("cloud_cover")
        cloud = None if cloud_raw is None else float(cloud_raw)
        if max_cloud_cover is not None and cloud is not None and cloud > max_cloud_cover:
            reasons.append(f"cloud-cover:{cloud:g}")
        if reasons:
            skipped[item_id] = tuple(reasons)
            continue
        acquired = exemplar["acquisition_time"]
        if acquired.tzinfo is None:
            acquired = acquired.replace(tzinfo=timezone.utc)
        tile = str(exemplar.get("tile_id") or item_id)
        selected.setdefault(tile, []).append(
            CopernicusMonthlyScene(
                item_id=item_id,
                tile_id=tile,
                acquisition_time=acquired.astimezone(timezone.utc),
                cloud_cover=cloud,
                assets={key: assets[key] for key in VEGETATION_L2A_ASSET_KEYS},
            )
        )
    for scenes in selected.values():
        scenes.sort(key=lambda scene: (scene.acquisition_time, scene.item_id))
    return dict(sorted(selected.items())), skipped


@dataclass(frozen=True)
class CopernicusMonthlyConfig:
    dataset_db: Path
    output_root: Path
    year: int
    month: int
    bbox: tuple[float, float, float, float] = field(
        default_factory=lambda: tuple(SWITZERLAND_BBOX)
    )
    cutline_path: Path | None = None
    target_crs: str = "EPSG:2056"
    resolution: float = 20.0
    max_cloud_cover: float | None = 100.0
    cloud_probability_threshold: float = 30.0
    invalid_scl_classes: tuple[int, ...] = DEFAULT_INVALID_SCL_CLASSES
    compression: str = "DEFLATE"
    nodata: float = DEFAULT_VEGETATION_NODATA
    warp_memory_mib: int = 256
    max_output_gib: float = 16.0
    overwrite: bool = False
    dry_run: bool = False
    allow_incomplete_month: bool = False
    staging_root: Path | None = None

    def __post_init__(self) -> None:
        calendar_month_window(self.year, self.month)
        object.__setattr__(self, "dataset_db", Path(self.dataset_db).expanduser())
        object.__setattr__(self, "output_root", Path(self.output_root).expanduser())
        object.__setattr__(self, "bbox", _validate_bbox(self.bbox))
        if self.cutline_path is not None:
            object.__setattr__(self, "cutline_path", Path(self.cutline_path).expanduser().resolve())
        if self.staging_root is not None:
            object.__setattr__(self, "staging_root", Path(self.staging_root).expanduser().resolve())
        if self.resolution <= 0:
            raise ValueError("resolution must be positive")
        if self.max_cloud_cover is not None and not 0 <= self.max_cloud_cover <= 100:
            raise ValueError("max_cloud_cover must be between 0 and 100")
        if not 0 <= self.cloud_probability_threshold <= 100:
            raise ValueError("cloud_probability_threshold must be between 0 and 100")
        if self.warp_memory_mib < 16:
            raise ValueError("warp_memory_mib must be at least 16")
        if self.max_output_gib <= 0:
            raise ValueError("max_output_gib must be positive")

    @property
    def month_directory(self) -> Path:
        return self.output_root / f"year={self.year:04d}" / f"month={self.month:02d}"

    @property
    def stem(self) -> str:
        resolution = f"{self.resolution:g}m".replace(".", "p")
        return f"switzerland_copernicus_l2a_{self.year:04d}_{self.month:02d}_{resolution}"


@dataclass(frozen=True)
class CopernicusMonthlyResult:
    reflectance_path: Path
    indices_path: Path
    manifest_path: Path
    width: int
    height: int
    scene_count: int
    timings_seconds: dict[str, float]
    dry_run: bool

    @property
    def elapsed_seconds(self) -> float:
        return self.timings_seconds.get("total", 0.0)


class CopernicusMonthlyProcessor:
    """Build one direct-CDSE L2A monthly median and four-index product."""

    def __init__(self, config: CopernicusMonthlyConfig) -> None:
        self.config = config
        if not config.dataset_db.is_file():
            raise FileNotFoundError(
                f"Copernicus dataset catalogue does not exist: {config.dataset_db}"
            )
        self.catalog = DatasetCatalog(config.dataset_db)
        self._gdal = (
            {}
            if config.dry_run
            else locate_gdal(
                "gdal_calc.py", "gdal_translate", "gdalwarp", "gdalbuildvrt", "gdalinfo"
            )
        )

    def _query(self) -> tuple[dict[str, list[CopernicusMonthlyScene]], dict[str, tuple[str, ...]]]:
        start, end = calendar_month_window(self.config.year, self.config.month)
        pieces = self.catalog.query_raster_pieces(
            bbox=self.config.bbox,
            start_datetime=start,
            end_datetime=end - timedelta(microseconds=1),
            asset_keys=VEGETATION_L2A_ASSET_KEYS,
        )
        return select_copernicus_scenes(pieces, max_cloud_cover=self.config.max_cloud_cover)

    def _warp_asset(
        self,
        *,
        piece: Mapping[str, Any],
        output: Path,
        extent: tuple[float, float, float, float] | None,
        resampling: str,
    ) -> None:
        command = [
            self._gdal["gdalwarp"],
            "-overwrite",
            "-of",
            "VRT",
            "-t_srs",
            self.config.target_crs,
            "-tr",
            str(self.config.resolution),
            str(self.config.resolution),
            "-tap",
            "-r",
            resampling,
            "-ot",
            "Float32",
            "-dstnodata",
            str(self.config.nodata),
            "-wm",
            str(self.config.warp_memory_mib),
            "-multi",
        ]
        if piece.get("nodata") is not None:
            command.extend(("-srcnodata", str(piece["nodata"])))
        if extent is not None:
            command.extend(("-te", *(str(value) for value in extent)))
        command.extend((str(piece["local_path"]), str(output)))
        run_gdal(command)

    @staticmethod
    def _physical_transform(piece: Mapping[str, Any]) -> tuple[float, float]:
        scale = piece.get("raster_scale")
        offset = piece.get("raster_offset")
        if scale is None or offset is None:
            raise ValueError(
                "Copernicus L2A reflectance requires explicit raster scale and offset "
                f"metadata; missing for {piece.get('item_id')} {piece.get('asset_key')}"
            )
        physical_scale = float(scale)
        physical_offset = float(offset)
        if (
            not math.isfinite(physical_scale)
            or physical_scale <= 0
            or not math.isfinite(physical_offset)
        ):
            raise ValueError(
                "Invalid Copernicus L2A scale/offset for "
                f"{piece.get('item_id')} {piece.get('asset_key')}"
            )
        return physical_scale, physical_offset

    def _stage_local(self, piece: Mapping[str, Any], staging_dir: Path) -> dict[str, Any]:
        """Copy one band to the persistent local staging root and resume safely.

        Reading every band straight from the network share for each warp and
        median pass is the dominant cost.  Staging each file once to a local
        volume makes the compute local and lets an interrupted run resume by
        skipping files that already finished copying (marked by byte size).
        """

        source = Path(str(piece["local_path"]))
        if self.config.staging_root is None:
            return dict(piece)
        destination = staging_dir / source.name
        expected = source.stat().st_size
        if not (destination.exists() and destination.stat().st_size == expected):
            staging_dir.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
            shutil.copyfile(source, temporary)
            if temporary.stat().st_size != expected:
                temporary.unlink(missing_ok=True)
                raise IOError(f"Incomplete staged copy of {source.name}")
            os.replace(temporary, destination)
        staged = dict(piece)
        staged["local_path"] = destination
        return staged

    def _scene_reflectance(self, scene: CopernicusMonthlyScene, directory: Path) -> Path:
        directory.mkdir(parents=True)
        warped: dict[str, Path] = {}
        common_extent: tuple[float, float, float, float] | None = None
        for key in VEGETATION_L2A_ASSET_KEYS:
            output = directory / f"{key}.vrt"
            staged_piece = self._stage_local(
                scene.assets[key],
                (self.config.staging_root or directory) / "bands" / scene.item_id,
            )
            self._warp_asset(
                piece=staged_piece,
                output=output,
                extent=common_extent,
                resampling="near" if key in COPERNICUS_QUALITY_ASSETS else "bilinear",
            )
            if common_extent is None:
                common_extent = raster_extent(inspect_raster(self._gdal["gdalinfo"], output))
            warped[key] = output

        variables = dict(
            zip(VEGETATION_L2A_ASSET_KEYS, _VARIABLES[: len(VEGETATION_L2A_ASSET_KEYS)], strict=True)
        )
        scl = variables["SCL_20m"]
        cld = variables["CLD_20m"]
        transforms = {
            key: self._physical_transform(scene.assets[key])
            for key in COPERNICUS_REFLECTANCE_ASSETS
        }
        validity = [
            f"{variables[key]}!={self.config.nodata:g}" for key in COPERNICUS_REFLECTANCE_ASSETS
        ]
        validity.extend(
            (
                f"({variables[key]}*{transforms[key][0]!r}+"
                f"{transforms[key][1]!r})>={COPERNICUS_MINIMUM_PHYSICAL_REFLECTANCE:g}"
            )
            for key in COPERNICUS_REFLECTANCE_ASSETS
        )
        validity.extend(f"{scl}!={value}" for value in sorted(set(self.config.invalid_scl_classes)))
        validity.extend((f"{cld}>=0", f"{cld}<={self.config.cloud_probability_threshold:g}"))
        valid = "logical_and.reduce((" + ",".join(validity) + "))"
        output = directory / "reflectance.tif"
        command = [self._gdal["gdal_calc.py"]]
        for key, variable in variables.items():
            command.extend((f"-{variable}", str(warped[key]), f"--{variable}_band", "1"))
        for key in COPERNICUS_REFLECTANCE_ASSETS:
            variable = variables[key]
            scale, offset = transforms[key]
            command.extend(
                (
                    "--calc",
                    f"where({valid},{variable}*{scale!r}+{offset!r},{self.config.nodata:g})",
                )
            )
        command.extend(
            (
                "--outfile", str(output), "--NoDataValue", str(self.config.nodata),
                "--type", "Float32", "--format", "GTiff", "--co",
                f"COMPRESS={self.config.compression}", "--co", "BIGTIFF=YES", "--co",
                "TILED=YES", "--hideNoData", "--projectionCheck", "--overwrite", "--quiet",
            )
        )
        run_gdal(command)
        return output

    def _country_vrt(self, tile_paths: Sequence[Path], directory: Path) -> Path:
        tiles = directory / "tiles.vrt"
        run_gdal(
            (
                self._gdal["gdalbuildvrt"], "-overwrite", "-srcnodata", str(self.config.nodata),
                "-vrtnodata", str(self.config.nodata), str(tiles), *(str(path) for path in tile_paths),
            )
        )
        country = directory / "country.vrt"
        west, south, east, north = self.config.bbox
        command = [
            self._gdal["gdalwarp"], "-overwrite", "-of", "VRT", "-t_srs",
            self.config.target_crs, "-te_srs", "EPSG:4326", "-te", str(west), str(south),
            str(east), str(north), "-tr", str(self.config.resolution), str(self.config.resolution),
            "-tap", "-r", "bilinear", "-srcnodata", str(self.config.nodata), "-dstnodata",
            str(self.config.nodata), "-wm", str(self.config.warp_memory_mib), "-multi",
        ]
        if self.config.cutline_path is not None:
            command.extend(("-cutline", str(self.config.cutline_path), "-crop_to_cutline"))
        command.extend((str(tiles), str(country)))
        run_gdal(command)
        set_vrt_band_descriptions(country, REFLECTANCE_BAND_NAMES)
        return country

    def _manifest(
        self,
        *,
        scenes_by_tile: Mapping[str, Sequence[CopernicusMonthlyScene]],
        skipped: Mapping[str, Sequence[str]],
        reflectance: Path,
        indices: Path,
        width: int,
        height: int,
        timings: Mapping[str, float],
    ) -> dict[str, Any]:
        scenes = [scene for tile in sorted(scenes_by_tile) for scene in scenes_by_tile[tile]]
        clouds = [scene.cloud_cover for scene in scenes if scene.cloud_cover is not None]
        return {
            "schema_version": 1,
            "processing_option": "direct-copernicus-monthly-composite",
            "source": "copernicus-sentinel-2-l2a",
            "source_is_force": False,
            "source_is_swisseo": False,
            "source_is_treeai": False,
            "formula_reference": "https://www.treeai.earth/swiss-vegetation-condition",
            "year": self.config.year,
            "month": self.config.month,
            "dataset_db": str(self.config.dataset_db.resolve()),
            "bbox_wgs84": list(self.config.bbox),
            "cutline_path": None if self.config.cutline_path is None else str(self.config.cutline_path),
            "target_crs": self.config.target_crs,
            "resolution": self.config.resolution,
            "composite_method": "SCL/CLD-screened per-pixel median of L2A surface reflectance",
            "input_location": "local Copernicus L2A assets indexed by DatasetCatalog",
            "timing_scope": {
                "monthly_mosaic_generation": (
                    "local alignment, SCL/CLD masking, per-tile temporal medians, national "
                    "mosaic/clip and reflectance COG writing; upstream retrieval excluded"
                ),
                "index_calculation": "combined NDVI/GNDVI/NDRE/NDWI calculation and COG writing",
                "total": "source selection plus both measured processing stages; retrieval excluded",
            },
            "native_input_assets": list(VEGETATION_L2A_ASSET_KEYS),
            "reflectance_band_order": list(REFLECTANCE_BAND_NAMES),
            "index_band_order": list(VEGETATION_INDEX_NAMES),
            "index_definitions": VEGETATION_INDEX_DEFINITIONS,
            "scene_count": len(scenes),
            "tile_count": len(scenes_by_tile),
            "average_catalog_cloud_cover_pct": None if not clouds else sum(clouds) / len(clouds),
            "quality_policy": {
                "max_scene_cloud_cover": self.config.max_cloud_cover,
                "maximum_CLD_probability": self.config.cloud_probability_threshold,
                "invalid_SCL_classes": list(self.config.invalid_scl_classes),
                "minimum_physical_reflectance": COPERNICUS_MINIMUM_PHYSICAL_REFLECTANCE,
            },
            "sources": [
                {
                    "item_id": scene.item_id,
                    "tile_id": scene.tile_id,
                    "acquisition_time": _iso_z(scene.acquisition_time),
                    "cloud_cover": scene.cloud_cover,
                    "physical_reflectance_transforms": {
                        key: {
                            "scale": self._physical_transform(scene.assets[key])[0],
                            "offset": self._physical_transform(scene.assets[key])[1],
                        }
                        for key in COPERNICUS_REFLECTANCE_ASSETS
                    },
                }
                for scene in scenes
            ],
            "skipped_items": {key: list(value) for key, value in skipped.items()},
            "width": width,
            "height": height,
            "dry_run": self.config.dry_run,
            "outputs": {"reflectance": str(reflectance), "indices": str(indices)},
            "timings_seconds": {key: round(float(value), 3) for key, value in timings.items()},
        }

    def run(self) -> CopernicusMonthlyResult:
        started = time.perf_counter()
        _, end = calendar_month_window(self.config.year, self.config.month)
        if end > datetime.now(timezone.utc) and not self.config.allow_incomplete_month:
            raise ValueError("Requested calendar month is incomplete")
        if self.config.cutline_path is not None and not self.config.cutline_path.is_file():
            raise FileNotFoundError(f"Cutline does not exist: {self.config.cutline_path}")
        stage = time.perf_counter()
        scenes_by_tile, skipped = self._query()
        timings: dict[str, float] = {"source_selection": time.perf_counter() - stage}
        if not scenes_by_tile:
            raise FileNotFoundError("No complete local Copernicus vegetation-profile scenes found")
        output_dir = self.config.month_directory.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        reflectance = output_dir / f"{self.config.stem}_reflectance.tif"
        indices = output_dir / f"{self.config.stem}_indices.tif"
        manifest = output_dir / f"{self.config.stem}.manifest.json"
        width, height = _approximate_dimensions(self.config.bbox, self.config.resolution)
        estimated = width * height * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES)) * _FLOAT32_BYTES
        if estimated / (1024**3) > self.config.max_output_gib:
            raise ValueError("Estimated uncompressed output exceeds max_output_gib")
        if self.config.dry_run:
            timings["monthly_mosaic_generation"] = 0.0
            timings["index_calculation"] = 0.0
            timings["total"] = time.perf_counter() - started
            _write_json_atomic(
                manifest,
                self._manifest(
                    scenes_by_tile=scenes_by_tile, skipped=skipped, reflectance=reflectance,
                    indices=indices, width=width, height=height, timings=timings,
                ),
            )
            return CopernicusMonthlyResult(
                reflectance, indices, manifest, width, height,
                sum(len(value) for value in scenes_by_tile.values()), timings, True,
            )
        existing = [path for path in (reflectance, indices) if path.exists()]
        if existing and not self.config.overwrite:
            raise FileExistsError("Outputs exist; pass --overwrite")
        # When a local staging root is configured, run all intermediate and
        # partial-COG work there and only copy finished products to the output
        # volume.  This keeps random-seek GeoTIFF writes off the (possibly
        # intermittent) network share and lets a drop only cost a re-copy.
        work_root = (
            output_dir
            if self.config.staging_root is None
            else self.config.staging_root / "work" / self.config.stem
        )
        work_root.mkdir(parents=True, exist_ok=True)
        partial_reflectance = work_root / f"{reflectance.stem}.partial.tif"
        partial_indices = work_root / f"{indices.stem}.partial.tif"
        if self.config.overwrite:
            partial_reflectance.unlink(missing_ok=True)
            partial_indices.unlink(missing_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix="terravault-copernicus-monthly-", dir=work_root
            ) as temporary:
                temp = Path(temporary)
                mosaic_started = time.perf_counter()
                tiles: list[Path] = []
                for tile, scenes in scenes_by_tile.items():
                    scene_root = temp / "scenes" / tile
                    inputs = [self._scene_reflectance(scene, scene_root / scene.item_id) for scene in scenes]
                    tile_output = temp / "tiles" / f"{tile}.tif"
                    tile_output.parent.mkdir(parents=True, exist_ok=True)
                    write_multiband_median(
                        input_paths=inputs, output_path=tile_output, band_names=REFLECTANCE_BAND_NAMES,
                        gdal=self._gdal, nodata=self.config.nodata, compression=self.config.compression,
                    )
                    tiles.append(tile_output)
                    shutil.rmtree(scene_root, ignore_errors=True)
                country = self._country_vrt(tiles, temp)
                info = inspect_raster(self._gdal["gdalinfo"], country)
                width, height = (int(value) for value in info["size"])
                exact_estimated = (
                    width
                    * height
                    * (len(REFLECTANCE_BAND_NAMES) + len(VEGETATION_INDEX_NAMES))
                    * _FLOAT32_BYTES
                )
                if exact_estimated / (1024**3) > self.config.max_output_gib:
                    raise ValueError("Exact uncompressed output exceeds max_output_gib")
                write_described_cog(
                    source_path=country, output_path=partial_reflectance,
                    band_names=REFLECTANCE_BAND_NAMES, gdal=self._gdal,
                    compression=self.config.compression,
                    metadata={"TERRAVAULT_SOURCE": "copernicus-sentinel-2-l2a"},
                )
                timings["monthly_mosaic_generation"] = time.perf_counter() - mosaic_started
                stage = time.perf_counter()
                write_vegetation_indices(
                    reflectance_path=partial_reflectance, output_path=partial_indices,
                    gdal=self._gdal, nodata=self.config.nodata, compression=self.config.compression,
                    metadata={"TERRAVAULT_SOURCE": "copernicus-sentinel-2-l2a"},
                )
                timings["index_calculation"] = time.perf_counter() - stage
            if self.config.staging_root is None:
                os.replace(partial_reflectance, reflectance)
                os.replace(partial_indices, indices)
            else:
                shutil.copyfile(partial_reflectance, reflectance)
                shutil.copyfile(partial_indices, indices)
        finally:
            if self.config.staging_root is None:
                partial_reflectance.unlink(missing_ok=True)
                partial_indices.unlink(missing_ok=True)
        timings["total"] = time.perf_counter() - started
        _write_json_atomic(
            manifest,
            self._manifest(
                scenes_by_tile=scenes_by_tile, skipped=skipped, reflectance=reflectance,
                indices=indices, width=width, height=height, timings=timings,
            ),
        )
        return CopernicusMonthlyResult(
            reflectance, indices, manifest, width, height,
            sum(len(value) for value in scenes_by_tile.values()), timings, False,
        )

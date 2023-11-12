import shutil
from pathlib import Path

import hydra
import numpy as np
import polars as pl
from scipy.stats import norm
from tqdm import tqdm

from src.conf import PrepareDataConfig
from src.utils.common import trace

SERIES_SCHEMA = {
    "series_id": pl.Utf8,
    "step": pl.UInt32,
    "anglez": pl.Float32,
    "enmo": pl.Float32,
}

FEATURE_NAMES = [
    "anglez",
    "enmo",
    "hour_sin",
    "hour_cos",
    "signal_awake",
    "signal_onset",
    "signal_poly",
]

# FEATURE_NAMES = [
#     "anglez",
#     "enmo",
#     "step",
#     "hour_sin",
#     "hour_cos",
#     "month_sin",
#     "month_cos",
#     "minute_sin",
#     "minute_cos",
#     "anglez_sin",
#     "anglez_cos",
# ]

ANGLEZ_MEAN = -8.810476
ANGLEZ_STD = 35.521877
ENMO_MEAN = 0.041315
ENMO_STD = 0.101829



# 新しい特徴量の辞書を定義
onset_features = {
    'w1': 0.97292814,
    'mu1': 2.12861738,
    'sigma1': 1.699476,
    'mu2': 22.7643724,
    'sigma2': 0.42483832
}

awake_features = {
    'w1': 0.47799486,
    'mu1': 10.89539674,
    'sigma1': 0.87151052,
    'mu2': 11.82689931,
    'sigma2': 2.06792452
}

# 3次多項式のパラメータ
coefficients = [0.7498337722857453, -0.29120336781669365, 0.0325499456141388, -0.000865744935362603]

# 3次多項式の値を計算する関数を定義
def poly_fit(time):
    return coefficients[0] + coefficients[1] * time + coefficients[2] * time**2 + coefficients[3] * time**3

def calc_mixture_gaussian(time, w1, mu1, sigma1, mu2, sigma2):
    return w1 * norm.pdf(time, mu1, sigma1) + (1 - w1) * norm.pdf(time, mu2, sigma2)

# 新しい特徴量の辞書を生成
new_time_units = np.arange(0, 240)  # 仮定
signal_awake_dict = {hour: calc_mixture_gaussian(hour / 10, **awake_features) for hour in new_time_units}
signal_onset_dict = {hour: calc_mixture_gaussian(hour / 10, **onset_features) for hour in new_time_units}
signal_poly_dict = {hour: poly_fit(hour / 10) for hour in new_time_units}


def to_coord(x: pl.Expr, max_: int, name: str) -> list[pl.Expr]:
    rad = 2 * np.pi * (x % max_) / max_
    x_sin = rad.sin()
    x_cos = rad.cos()

    return [x_sin.alias(f"{name}_sin"), x_cos.alias(f"{name}_cos")]


# def deg_to_rad(x: pl.Expr) -> pl.Expr:
#     return np.pi / 180 * x

# add_feature 関数に新しい特徴量を計算するコードを組み込む
def add_feature(series_df: pl.DataFrame) -> pl.DataFrame:
    # 既存の特徴量の計算
    series_df = series_df.with_columns(
        *to_coord(pl.col("timestamp").dt.hour(), 24, "hour"),
    )
    
    # 新しい特徴量の計算を追加
    hour_plus_minute = (pl.col("timestamp").dt.hour() * 10 + pl.col("timestamp").dt.minute() // 6)
    series_df = series_df.with_columns(
        pl.Series(hour_plus_minute.map(signal_awake_dict)).alias("signal_awake"),
        pl.Series(hour_plus_minute.map(signal_onset_dict)).alias("signal_onset"),
        pl.Series(hour_plus_minute.map(signal_poly_dict)).alias("signal_poly")
    )
    
    return series_df.select("series_id", *FEATURE_NAMES)


# def add_feature(series_df: pl.DataFrame) -> pl.DataFrame:
#     series_df = (
#         series_df.with_row_count("step")
#         .with_columns(
#             *to_coord(pl.col("timestamp").dt.hour(), 24, "hour"),
#             *to_coord(pl.col("timestamp").dt.month(), 12, "month"),
#             *to_coord(pl.col("timestamp").dt.minute(), 60, "minute"),
#             pl.col("step") / pl.count("step"),
#             pl.col('anglez_rad').sin().alias('anglez_sin'),
#             pl.col('anglez_rad').cos().alias('anglez_cos'),
#         )
#         .select("series_id", *FEATURE_NAMES)
#     )
#     return series_df


def save_each_series(this_series_df: pl.DataFrame, columns: list[str], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    for col_name in columns:
        x = this_series_df.get_column(col_name).to_numpy(zero_copy_only=True)
        np.save(output_dir / f"{col_name}.npy", x)


@hydra.main(config_path="conf", config_name="prepare_data", version_base="1.2")
def main(cfg: PrepareDataConfig):
    processed_dir: Path = Path(cfg.dir.processed_dir) / cfg.phase

    # ディレクトリが存在する場合は削除
    if processed_dir.exists():
        shutil.rmtree(processed_dir)
        print(f"Removed {cfg.phase} dir: {processed_dir}")

    with trace("Load series"):
        # scan parquet
        if cfg.phase in ["train", "test"]:
            series_lf = pl.scan_parquet(
                Path(cfg.dir.data_dir) / f"{cfg.phase}_series.parquet",
                low_memory=True,
            )
        elif cfg.phase == "dev":
            series_lf = pl.scan_parquet(
                Path(cfg.dir.processed_dir) / f"{cfg.phase}_series.parquet",
                low_memory=True,
            )
        else:
            raise ValueError(f"Invalid phase: {cfg.phase}")

        # preprocess
        series_df = (
            series_lf.with_columns(
                pl.col("timestamp").str.to_datetime("%Y-%m-%dT%H:%M:%S%z"),
                deg_to_rad(pl.col("anglez")).alias("anglez_rad"),
                (pl.col("anglez") - ANGLEZ_MEAN) / ANGLEZ_STD,
                (pl.col("enmo") - ENMO_MEAN) / ENMO_STD,
            )
            .select(
                [
                    pl.col("series_id"),
                    pl.col("anglez"),
                    pl.col("enmo"),
                    pl.col("timestamp"),
                    pl.col("anglez_rad"),
                ]
            )
            .collect(streaming=True)
            .sort(by=["series_id", "timestamp"])
        )
        n_unique = series_df.get_column("series_id").n_unique()
    with trace("Save features"):
        for series_id, this_series_df in tqdm(series_df.group_by("series_id"), total=n_unique):
            # 特徴量を追加
            this_series_df = add_feature(this_series_df)

            # 特徴量をそれぞれnpyで保存
            series_dir = processed_dir / series_id  # type: ignore
            save_each_series(this_series_df, FEATURE_NAMES, series_dir)


if __name__ == "__main__":
    main()

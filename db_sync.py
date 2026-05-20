#!/usr/bin/env python3
"""Cloudflare R2 数据库同步工具（R2 兼容 S3 协议）"""
import os
import logging
from pathlib import Path

log = logging.getLogger("scanner")

DB_LOCAL  = Path(__file__).parent / "data" / "nasdaq_history.db"
R2_BUCKET = "nasdaq-hunter"
R2_KEY    = "nasdaq_history.db"


def _client():
    import boto3
    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        region_name="auto",
    )


def download_db():
    """从 R2 下载最新 DB 到本地 data/ 目录"""
    DB_LOCAL.parent.mkdir(parents=True, exist_ok=True)
    log.info("从 R2 下载数据库…")
    _client().download_file(R2_BUCKET, R2_KEY, str(DB_LOCAL))
    size_mb = DB_LOCAL.stat().st_size / 1024 / 1024
    log.info("数据库下载完成: %.1f MB", size_mb)


def upload_db():
    """将本地 DB 上传回 R2"""
    if not DB_LOCAL.exists():
        log.warning("本地 DB 不存在，跳过上传")
        return
    size_mb = DB_LOCAL.stat().st_size / 1024 / 1024
    log.info("上传数据库到 R2 (%.1f MB)…", size_mb)
    _client().upload_file(str(DB_LOCAL), R2_BUCKET, R2_KEY)
    log.info("数据库上传完成")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "download":
        download_db()
    elif cmd == "upload":
        upload_db()
    else:
        print("用法: python3 db_sync.py download|upload")

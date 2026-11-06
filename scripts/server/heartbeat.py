#!/usr/bin/env python3
"""Reports when each product's data was last written, for the alert function to check.

Runs on the server once a minute. It only reports: the decision to alert is made off the server,
so that a dead server still raises an alert. Uses the system Python and only the standard library.
"""

import contextlib
import json
import os
import shutil
import subprocess
import time


def newest_write(data_dir, product):
    roots = [os.path.join(data_dir, ".spool", product), os.path.join(data_dir, product)]
    newest = 0
    for root in roots:
        for directory, _subdirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".parquet"):
                    continue
                # The daily merge may remove a part after it was listed.
                with contextlib.suppress(FileNotFoundError):
                    newest = max(newest, int(os.stat(os.path.join(directory, name)).st_mtime))
    return newest


def main():
    data_dir = os.environ["DATA_DIR"]
    products = os.environ["TOUCHSTONE_PRODUCTS"].split(",")
    body = {
        "written_at": int(time.time()),
        "products": {product: newest_write(data_dir, product) for product in products},
        "disk_free_bytes": shutil.disk_usage(data_dir).free,
    }
    destination = "s3://{}/ops/heartbeat.json".format(os.environ["TOUCHSTONE_BUCKET"])
    subprocess.run(
        ["aws", "s3", "cp", "-", destination, "--only-show-errors"],
        input=json.dumps(body).encode(),
        check=True,
    )


if __name__ == "__main__":
    main()

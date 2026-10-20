import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="拼多多商品采集 MVP")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    directory = os.getenv("PDD_DATA_DIR", ".local/pdd-mvp")
    print(
        f"打开 http://127.0.0.1:{args.port}；首次启动后从 {directory}/access-token 读取访问口令。"
    )
    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=args.port, workers=1)


if __name__ == "__main__":
    main()

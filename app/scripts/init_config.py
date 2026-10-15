"""首次启动时生成一份默认配置。

由「启动.bat」调用，这样默认值就直接复用 backend.config.DEFAULT_CONFIG，
不用在 .bat 里手写 JSON（转义太容易出错）。

用法：python app/scripts/init_config.py <默认笔记目录>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import CONFIG_PATH, DEFAULT_CONFIG, save_config  # noqa: E402


def main() -> int:
    notes_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = dict(DEFAULT_CONFIG)
    if notes_dir:
        cfg["save_path"] = os.path.abspath(notes_dir)

    save_config(cfg)
    os.makedirs(cfg["save_path"], exist_ok=True)

    print(f"     配置已写入 {CONFIG_PATH}")
    print(f"     笔记目录   {cfg['save_path']}")
    print(f"     主题       {cfg['theme']}    笔记类型 {cfg['note_style']}")
    print("     还需要在界面「设置」里填 DeepSeek API Key 才能生成笔记。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

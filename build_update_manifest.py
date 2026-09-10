"""为已构建的正式安装包生成公开更新清单（不依赖 GitHub API 查询额度）。"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

RELEASES = 'https://github.com/liw56747-sys/vocab-harvester/releases'


def build_manifest(version: str, windows_file: Path, macos_file: Path, notes: str) -> dict:
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('更新清单仅接受正式版本号')
    expected = [f'vocab-harvester-{version}-setup.exe', f'vocab-harvester-{version}.dmg']
    assets = []
    for file, name in zip((windows_file, macos_file), expected):
        if file.name != name or not file.is_file() or file.stat().st_size == 0:
            raise ValueError(f'安装包不存在、为空或版本不匹配: {name}')
        assets.append({'name': name, 'size': file.stat().st_size,
                       'browser_download_url': f'{RELEASES}/download/v{version}/{name}'})
    return {'tag_name': f'v{version}', 'draft': False, 'prerelease': False,
            'html_url': f'{RELEASES}/tag/v{version}', 'body': notes, 'assets': assets}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--windows-file', type=Path, required=True)
    parser.add_argument('--macos-file', type=Path, required=True)
    parser.add_argument('--notes-file', type=Path, default=Path('release_notes.md'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_manifest(args.version, args.windows_file, args.macos_file,
                            args.notes_file.read_text(encoding='utf-8'))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()

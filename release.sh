#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# release.sh — 本地发布 GitHub Release（含自动更新说明）
#
# 用法:
#   ./release.sh              # 使用 VERSION 文件中的版本号
#   ./release.sh 1.2.4        # 指定版本号
#
# 前置条件:
#   - gh CLI 已安装并登录 (gh auth login)
#   - 代码已 commit 并 push 到 origin
#   - 已创建并推送对应的 git tag
# ─────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 确定版本号 ──
if [ -n "${1:-}" ]; then
    VERSION="$1"
else
    VERSION="$(head -1 VERSION | tr -d '[:space:]')"
fi
TAG="v${VERSION}"

echo "============================================"
echo "  发布 vocab-harvester ${TAG}"
echo "============================================"
echo ""

# ── 检查 gh CLI ──
if ! command -v gh &>/dev/null; then
    echo "[ERROR] gh CLI 未安装。请先安装："
    echo "  brew install gh        # macOS"
    echo "  https://cli.github.com # 其他平台"
    exit 1
fi

# ── 检查登录状态 ──
if ! gh auth status &>/dev/null 2>&1; then
    echo "[ERROR] gh 未登录。请执行: gh auth login"
    exit 1
fi

# ── 检查 tag 是否存在 ──
if ! git rev-parse "$TAG" &>/dev/null; then
    echo "[ERROR] Tag ${TAG} 不存在。请先创建并推送："
    echo "  git tag ${TAG}"
    echo "  git push origin ${TAG}"
    exit 1
fi

# ── 检查 tag 是否已推送到远程 ──
if ! git ls-remote --tags origin | grep -q "$TAG"; then
    echo "[WARN] Tag ${TAG} 尚未推送到远程，正在推送..."
    git push origin "$TAG"
fi

# ── 获取上一个 tag ──
PREV_TAG="$(git describe --tags --abbrev=0 "${TAG}^" 2>/dev/null || echo "")"

if [ -n "$PREV_TAG" ]; then
    echo "[*] 对比范围: ${PREV_TAG} → ${TAG}"
    # 获取两个 tag 之间的提交记录
    CHANGELOG="$(git log "${PREV_TAG}..${TAG}" --pretty=format:'- %s' --no-merges)"
else
    echo "[*] 首次发布，无历史版本对比"
    CHANGELOG="$(git log --pretty=format:'- %s' --no-merges -20)"
fi

if [ -z "$CHANGELOG" ]; then
    CHANGELOG="- 无变更说明"
fi

# ── 生成更新说明 ──
RELEASE_NOTES="## 更新内容

${CHANGELOG}

## 安装包

| 平台 | 文件 | 说明 |
|------|------|------|
| macOS | vocab-harvester-${VERSION}.dmg | 双击打开，拖入 Applications |
| Windows | vocab-harvester-${VERSION}-setup.exe | 双击安装向导 |

---
*完整提交记录: https://github.com/liw56747-sys/vocab-harvester/compare/${PREV_TAG:-main}...${TAG}*
"

echo ""
echo "── 更新说明 ──"
echo "$RELEASE_NOTES"
echo "──────────────"
echo ""

# ── 检查 Release 是否已存在 ──
if gh release view "$TAG" &>/dev/null 2>&1; then
    echo "[*] Release ${TAG} 已存在，更新更新说明..."
    gh release edit "$TAG" --notes "$RELEASE_NOTES" --title "vocab-harvester ${TAG}" --latest
    echo "[OK] Release 更新说明已更新"
else
    echo "[*] 创建 GitHub Release ${TAG}..."
    gh release create "$TAG" \
        --title "vocab-harvester ${TAG}" \
        --notes "$RELEASE_NOTES" \
        --latest
    echo "[OK] Release 已创建"
fi

echo ""
echo "============================================"
echo "  发布完成！"
echo "  https://github.com/liw56747-sys/vocab-harvester/releases/tag/${TAG}"
echo "============================================"

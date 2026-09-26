"""针对白色拼图缺口的离线定位；不访问网页，不读取登录信息。"""

from dataclasses import dataclass
import argparse
import json

import cv2
import numpy as np
from PIL import Image


class DetectionError(ValueError):
    """证据不足时拒绝生成鼠标动作。"""


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self):
        return self.x + self.w

    @property
    def bottom(self):
        return self.y + self.h

    @property
    def center(self):
        return (self.x + self.w // 2, self.y + self.h // 2)

    def crop(self, pixels):
        return pixels[self.y:self.bottom, self.x:self.right]


@dataclass(frozen=True)
class Detection:
    image: Rect
    gap: Rect
    handle: Rect
    track_right: int
    shape_score: float
    gap_center: tuple  # 缺口真实中心（轮廓矩心）

    @property
    def distance(self):
        # 水平位移 = 缺口真实中心 - 滑块中心
        return self.gap_center[0] - self.handle.center[0]

    def points(self, offset=(0, 0), correction=0):
        # 使用滑块中心作为起点
        x, y = self.handle.center
        end_x = x + self.distance + correction
        if not x < end_x < self.track_right - self.handle.w // 2 + 2:
            raise DetectionError("目标超出滑轨，已取消操作。")
        ox, oy = offset
        return (x + ox, y + oy), (end_x + ox, y + oy)


def puzzle_mask(body=50, radius=8, rise=11):
    """样例的轮廓：上、右凸起，左、下凹槽。"""
    mask = np.zeros((body + rise + 2, body + radius + 2), np.uint8)
    cv2.rectangle(mask, (0, rise), (body, rise + body), 255, -1)
    cv2.circle(mask, (body // 2, radius), radius, 255, -1)
    cv2.circle(mask, (body, rise + body // 2), radius, 255, -1)
    cv2.circle(mask, (0, rise + body // 2), radius, 0, -1)
    cv2.circle(mask, (body // 2, rise + body), radius, 0, -1)
    x, y, w, h = cv2.boundingRect(mask)
    return mask[y:y+h, x:x+w]


_TEMPLATES = [
    cv2.resize(puzzle_mask(50, radius, rise), (80, 80), interpolation=cv2.INTER_NEAREST) > 0
    for radius in (6, 7, 8, 9) for rise in (9, 10, 11, 12)
]


def shape_score(mask):
    normalized = cv2.resize(mask, (80, 80), interpolation=cv2.INTER_NEAREST) > 0
    return max(float(np.logical_and(normalized, t).sum() /
                     max(1, np.logical_or(normalized, t).sum())) for t in _TEMPLATES)


def _runs(values):
    padded = np.pad(np.asarray(values, dtype=np.int8), (1, 1))
    edges = np.diff(padded)
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


def _white_mask(rgb):
    """白色像素判据：三通道都够亮且几乎无色差。"""
    return (rgb.min(axis=2) >= 250) & (rgb.max(axis=2) - rgb.min(axis=2) <= 5)


def _find_image(rgb, arrow):
    """从箭头位置向上扫描找到图片区域。"""
    width = rgb.shape[1]
    ax, ay, aw, ah = arrow.x, arrow.y, arrow.w, arrow.h
    colored = rgb.min(axis=2) < 235

    # 垂直扫描：在箭头水平范围附近找图片上下边界
    scan_left = max(0, ax - aw)
    scan_right = min(width, ax + aw * 2)
    band = colored[:, scan_left:scan_right]
    row_mask = band.mean(axis=1) > 0.3
    row_mask = cv2.morphologyEx(row_mask.astype(np.uint8)[:, None],
                                cv2.MORPH_CLOSE, np.ones((5, 1), np.uint8)).ravel() > 0
    above = [(t, b) for t, b in _runs(row_mask) if b <= ay + 2 and b - t > 20]
    if not above:
        return None
    top, bottom = max(above, key=lambda r: r[1] - r[0])

    # 水平扫描：用图片中间行找左右边界（缺口可能断开，取最左最右）
    mid_y = (top + bottom) // 2
    col_runs = _runs(colored[mid_y])
    if not col_runs:
        return None
    left, right = int(col_runs[0][0]), int(col_runs[-1][1])
    return right, Rect(left, int(top), right - left, int(bottom - top))


def detect(image):
    rgb = np.asarray(image.convert("RGB"))
    if min(rgb.shape[:2]) < 100:
        raise DetectionError("选区太小，请包含完整图片和底部滑轨。")

    # 深色箭头 = 滑块位置
    dark_mask = (rgb.max(axis=2) < 150).astype(np.uint8)
    dark_count, _, dark_stats, _ = cv2.connectedComponentsWithStats(dark_mask, 8)

    white = _white_mask(rgb).astype(np.uint8)

    candidates = []
    for label in range(1, dark_count):
        ax, ay, aw, ah, area = map(int, dark_stats[label])
        if not (30 <= area <= 3000 and 4 <= aw <= 80 and 4 <= ah <= 80):
            continue
        arrow = Rect(ax, ay, aw, ah)
        hs = int(round((aw * ah * 10) ** 0.5))
        geometry = _find_image(rgb, arrow)
        if geometry is not None:
            candidates.append((arrow, hs, *geometry))
    solutions = []
    for arrow, hs, right, photo in candidates:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(photo.crop(white), 8)
        for label in range(1, count):
            x, y, w, h, area = map(int, stats[label])
            if not (20 <= w <= 240 and 20 <= h <= 240):
                continue
            if not (.75 <= w/h <= 1.25 and .48 < area/(w*h) < .88):
                continue
            if not (.70 <= w / hs <= 2.00 and .70 <= h / hs <= 2.00):
                continue
            mask = (labels[y:y+h, x:x+w] == label).astype(np.uint8) * 255
            score = shape_score(mask)
            if score < .86:
                continue
            # 缺口真实中心 = 轮廓矩心（面积过滤已保证 m00 > 0）
            moments = cv2.moments(mask)
            gap_center = (photo.x + x + int(moments['m10'] / moments['m00']),
                          photo.y + y + int(moments['m01'] / moments['m00']))
            gap = Rect(photo.x+x, photo.y+y, w, h)
            solution = Detection(photo, gap, arrow, right, score, gap_center)
            # 中心距离验证：缺口中心应在滑轨范围内
            if not hs*.30 < solution.distance <= photo.w - (gap.w + arrow.w) // 2:
                continue
            try:
                solution.points()
            except DetectionError:
                continue
            solutions.append(solution)
    if not solutions:
        raise DetectionError("未找到可靠的白色拼图缺口和滑轨；请完整框选验证框，或保存诊断图。")
    # 按缺口位置分组，多个缺口 = 歧义，拒绝
    groups = {}
    for s in solutions:
        groups.setdefault((s.gap.x, s.gap.y, s.gap.w, s.gap.h), []).append(s)
    if len(groups) != 1:
        raise DetectionError("检测到多个可能目标；请缩小选区，只保留一个验证框。")
    # 同一缺口多个箭头，选最左边的（箭头始终在左下角）
    group = next(iter(groups.values()))
    return min(group, key=lambda s: s.handle.x)


def white_ratio(image, rect):
    """计算矩形区域内白色像素占比，用于拖动反馈判断缺口是否已被覆盖。"""
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    x1, y1 = max(0, rect[0]), max(0, rect[1])
    x2, y2 = min(width, rect[2]), min(height, rect[3])
    if x2 <= x1 or y2 <= y1:
        return 0.
    return float(_white_mask(rgb[y1:y2, x1:x2]).mean())


def main():
    parser = argparse.ArgumentParser(description="离线检查截图，不进行任何鼠标操作。")
    parser.add_argument("images", nargs="+")
    args = parser.parse_args()
    failed = False
    for path in args.images:
        try:
            with Image.open(path) as image:
                found = detect(image)
            print(json.dumps({"file": path, "image": vars(found.image),
                              "gap": vars(found.gap), "handle": vars(found.handle),
                              "distance": found.distance, "points": found.points(),
                              "shape_score": round(found.shape_score, 4)}, ensure_ascii=True))
        except (DetectionError, OSError) as exc:
            failed = True
            print(json.dumps({"file": path, "error": str(exc)}, ensure_ascii=True))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())

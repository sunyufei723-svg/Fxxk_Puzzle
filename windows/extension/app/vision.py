"""针对白色拼图缺口的离线定位；不访问网页，不读取登录信息。"""

from dataclasses import dataclass
from functools import lru_cache
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
    gap: Rect          # 缺口本体正方形（不含凸凹），即拼块该落到的那一格
    handle: Rect
    track_right: int
    shape_score: float
    gap_center: tuple  # 本体正方形中心

    @property
    def distance(self):
        # 水平位移 = 缺口本体中心 - 滑块中心
        return self.gap_center[0] - self.handle.center[0]

    def points(self, offset=(0, 0), correction=0):
        # 使用滑块中心作为起点
        x, y = self.handle.center
        end_x = x + self.distance + correction
        if not x < end_x < self.track_right - self.handle.w // 2 + 2:
            raise DetectionError("目标超出滑轨，已取消操作。")
        ox, oy = offset
        return (x + ox, y + oy), (end_x + ox, y + oy)


SIDES = ("top", "right", "bottom", "left")
# 四条边各自凸出或凹进，一共 16 种拼图块形状。
COMBINATIONS = [tuple(bits) for bits in np.ndindex(2, 2, 2, 2)]
BODIES = tuple(range(30, 97, 3))   # 本体正方形的边长
TAB = .18                         # 凸凹半圆半径 = 本体边长 × TAB
MIN_FIT = .88                     # 覆盖 × 边界吻合 的下限


@lru_cache(maxsize=None)
def _template(body, combination):
    """拼图块 = 正方形本体 + 四条边上各一个半圆，半圆要么凸出来要么凹进去。

    返回 (模板, 模板轮廓, 本体中心在模板里的坐标)。要对准的是本体，不是整块轮廓的
    中心——轮廓中心会随凸凹组合左右偏移。
    """
    radius = max(3, round(TAB * body))
    pad = radius + 1
    size = body + 2 * pad
    mask = np.zeros((size, size), np.uint8)
    cv2.rectangle(mask, (pad, pad), (pad + body - 1, pad + body - 1), 255, -1)
    middle = pad + body // 2
    edges = {'top': (middle, pad), 'bottom': (middle, pad + body - 1),
             'left': (pad, middle), 'right': (pad + body - 1, middle)}
    for side, raised in zip(SIDES, combination):
        cv2.circle(mask, edges[side], radius, 255 if raised else 0, -1)
    x, y, w, h = cv2.boundingRect(mask)
    mask = mask[y:y + h, x:x + w] > 0
    outline = _ring(mask.astype(np.uint8) * 255)
    return (mask.astype(np.float32),
            outline.astype(np.float32) / max(1., float(outline.sum())),
            (middle - x, middle - y))


def _ring(mask):
    """掩膜的边界：膨胀和腐蚀的差集（输入输出都是 0/255）。"""
    kernel = np.ones((3, 3), np.uint8)
    return cv2.dilate(mask, kernel) & ~cv2.erode(mask, kernel)


def fit_piece(mask):
    """把每种边长、每种凸凹组合的模板在白色掩膜上滑动，找最能解释这块白色的位置。

    两个判据相乘，缺一不可：
      覆盖度 = 模板盖住的像素里有多少是白的（模板被空白包住）
      吻合度 = 模板的边界上有多少落在白色的边界上（轮廓真的对得上）
    所以缺口旁边粘着一圈眼白不影响判定——模板边界该重合的地方照样重合；而一整块
    实心白虽然能把模板整个包住，模板边界却落在白的内部，吻合度直接归零。
    """
    height, width = mask.shape
    solid = (mask > 0).astype(np.uint8)
    if not solid.any():
        return 0.0, None, None, 0
    pixels = solid.astype(np.float32)
    # 本体正方形一定塞得下这块白色，而带凸凹的轮廓最多比它宽 36%，据此裁掉
    # 一大半边长。
    ys, xs = np.nonzero(solid)
    span = max(ys.max() - ys.min() + 1, xs.max() - xs.min() + 1)
    # 允许两像素抖动：真机截图的边缘有抗锯齿，开运算还会再削掉一像素。
    tolerance = (cv2.dilate(_ring(solid * 255), np.ones((5, 5), np.uint8)) > 0
                 ).astype(np.float32)
    best = None
    for body in BODIES:
        if not (.45 * span <= body <= span + 3):
            continue
        for combination in COMBINATIONS:
            template, outline, (centre_x, centre_y) = _template(body, combination)
            rows, cols = template.shape
            if rows > height or cols > width:
                continue
            covered = cv2.matchTemplate(pixels, template, cv2.TM_CCORR) / template.sum()
            aligned = cv2.matchTemplate(tolerance, outline, cv2.TM_CCORR)
            score = covered * aligned
            y, x = np.unravel_index(int(np.argmax(score)), score.shape)
            top = float(score[y, x])
            if best is None or top > best[0]:
                best = (top, x + centre_x, y + centre_y, body)
    if best is None:
        return 0.0, None, None, 0
    top, centre_x, centre_y, body = best
    return (top, Rect(centre_x - body // 2, centre_y - body // 2, body, body),
            (centre_x, centre_y), body)


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
    # 5x5 开运算先切断几像素宽的细桥：亮色底图上眼白、高光、白边框都够白，
    # 很容易和缺口连成一片。切不断的粗桥由 fit_piece 的边界吻合度去容忍。
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

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
            x, y, w, h, _ = map(int, stats[label])
            if not (20 <= w <= 240 and 20 <= h <= 240):
                continue
            # 模板最宽能到本体边长的 1.4 倍，先留够滑动余地再交给 fit_piece。
            pad = int(.45 * max(w, h)) + 4
            canvas = np.zeros((h + 2 * pad, w + 2 * pad), np.uint8)
            canvas[pad:pad + h, pad:pad + w] = (labels[y:y + h, x:x + w] == label) * 255
            score, body, centre, size = fit_piece(canvas)
            if score < MIN_FIT:
                continue
            if not (.55 <= size / hs <= 2.20):
                continue
            shift_x, shift_y = photo.x + x - pad, photo.y + y - pad
            solution = Detection(photo,
                                 Rect(body.x + shift_x, body.y + shift_y, body.w, body.h),
                                 arrow, right, score,
                                 (centre[0] + shift_x, centre[1] + shift_y))
            # 中心距离验证：缺口中心应在滑轨范围内
            if not (hs * .30 < solution.distance
                    <= photo.w - (solution.gap.w + arrow.w) // 2):
                continue
            try:
                solution.points()
            except DetectionError:
                continue
            solutions.append(solution)
    if not solutions:
        raise DetectionError("未找到可靠的白色拼图缺口和滑轨；请完整框选验证框，或保存诊断图。")
    # 同一个缺口会被多个候选箭头重复命中，按缺口中心聚成一簇；
    # 剩下不止一簇 = 选区里有多个缺口，无法判断该拖哪个。
    clusters = []
    for solution in solutions:
        x, y = solution.gap_center
        for cluster in clusters:
            ox, oy = cluster[0].gap_center
            if (x - ox) ** 2 + (y - oy) ** 2 <= (.6 * cluster[0].gap.w) ** 2:
                cluster.append(solution)
                break
        else:
            clusters.append([solution])
    if len(clusters) > 1:
        raise DetectionError("检测到多个可能目标；请缩小选区，只保留一个验证框。")
    # 同一缺口多个箭头，选最左边的（箭头始终在左下角）
    return min(clusters[0], key=lambda s: s.handle.x)


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

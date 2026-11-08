"""Raven's on-screen agent pointer for bare-metal runs on this Mac.

A borderless, click-through window above everything that glides to each action and ripples on
clicks. It is excluded from screen capture, so the agent never sees it. JSON lines on stdin:
{"op": "show"|"move"|"click"|"typing"|"hide", "x": .., "y": .., "count": ..}, in screen points
with the origin at the top left (like Quartz events). The helper exits when stdin closes.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

import AppKit  # type: ignore[import-not-found]
import Quartz  # type: ignore[import-not-found]
from PyObjCTools import AppHelper  # type: ignore[import-not-found]

SIZE = 120.0
GLIDE_S = 0.3
VIOLET = (0.545, 0.361, 0.965)
ARROW = [(0, 0), (0, -30), (8, -22), (14, -35), (20, -32), (14, -20), (24, -20)]


def _color(r: float, g: float, b: float, a: float = 1.0) -> Any:
    return Quartz.CGColorCreateGenericRGB(r, g, b, a)


def _polygon(points: list[tuple[float, float]], cx: float, cy: float) -> Any:
    path = Quartz.CGPathCreateMutable()
    for i, (x, y) in enumerate(points):
        (Quartz.CGPathMoveToPoint if i == 0 else Quartz.CGPathAddLineToPoint)(path, None, cx + x, cy + y)
    Quartz.CGPathCloseSubpath(path)
    return path


def _shape(path: Any, fill: Any, stroke: Any, width: float) -> Any:
    layer = Quartz.CAShapeLayer.layer()
    layer.setFrame_(AppKit.NSMakeRect(0, 0, SIZE, SIZE))
    layer.setPath_(path)
    layer.setFillColor_(fill)
    layer.setStrokeColor_(stroke)
    layer.setLineWidth_(width)
    return layer


class Pointer:
    def __init__(self) -> None:
        frame = AppKit.NSMakeRect(0, 0, SIZE, SIZE)
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False
        )
        win.setOpaque_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(True)
        win.setLevel_(AppKit.NSScreenSaverWindowLevel)
        win.setSharingType_(AppKit.NSWindowSharingNone)
        win.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle
        )
        view = AppKit.NSView.alloc().initWithFrame_(frame)
        view.setWantsLayer_(True)
        win.setContentView_(view)
        c = SIZE / 2
        ring = Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(c - 18, c - 18, 36, 36), None)
        self.ring = _shape(ring, _color(*VIOLET, 0.25), _color(*VIOLET), 3.0)
        self.ring.setOpacity_(0.0)
        self.arrow = _shape(_polygon(ARROW, c, c), _color(*VIOLET), _color(1, 1, 1), 2.0)
        self.arrow.setShadowColor_(_color(0, 0, 0))
        self.arrow.setShadowOpacity_(0.35)
        self.arrow.setShadowRadius_(3.0)
        self.arrow.setShadowOffset_((0, -1))
        view.layer().addSublayer_(self.ring)
        view.layer().addSublayer_(self.arrow)
        self.win = win

    def place(self, x: float, y: float, animate: bool) -> None:
        screen_h = AppKit.NSScreen.screens()[0].frame().size.height
        origin = AppKit.NSMakePoint(x - SIZE / 2, screen_h - y - SIZE / 2)
        if not (animate and self.win.isVisible()):
            self.win.setFrameOrigin_(origin)
            return
        AppKit.NSAnimationContext.beginGrouping()
        ctx = AppKit.NSAnimationContext.currentContext()
        ctx.setDuration_(GLIDE_S)
        ctx.setTimingFunction_(
            Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut)
        )
        self.win.animator().setFrameOrigin_(origin)
        AppKit.NSAnimationContext.endGrouping()

    def _animate(self, layer: Any, key: str, values: dict[str, tuple[float, float]], duration: float, delay: float = 0.0) -> None:
        animations = []
        for path, (start, end) in values.items():
            anim = Quartz.CABasicAnimation.animationWithKeyPath_(path)
            anim.setFromValue_(start)
            anim.setToValue_(end)
            animations.append(anim)
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_(animations)
        group.setDuration_(duration)
        group.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        group.setFillMode_(Quartz.kCAFillModeBackwards)
        layer.addAnimation_forKey_(group, key)

    def ripple(self, count: int) -> None:
        for i in range(max(1, min(count, 3))):
            self._animate(self.ring, f"ripple{i}", {"transform.scale": (0.3, 2.2), "opacity": (0.9, 0.0)}, 0.5, i * 0.18)

    def pulse(self) -> None:
        self._animate(self.arrow, "pulse", {"transform.scale": (1.15, 1.0)}, 0.25)

    def handle(self, cmd: dict[str, Any]) -> None:
        op = cmd.get("op")
        if op == "hide":
            self.win.orderOut_(None)
            return
        if "x" in cmd and "y" in cmd:
            self.place(float(cmd["x"]), float(cmd["y"]), animate=op == "move")
        self.win.orderFrontRegardless()
        if op == "click":
            self.ripple(int(cmd.get("count") or 1))
        elif op == "typing":
            self.pulse()


def _read_commands(pointer: Pointer) -> None:
    for line in sys.stdin:
        try:
            cmd = json.loads(line)
        except ValueError:
            continue
        if isinstance(cmd, dict):
            AppHelper.callAfter(pointer.handle, cmd)
    AppHelper.callAfter(AppHelper.stopEventLoop)


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    pointer = Pointer()
    threading.Thread(target=_read_commands, args=(pointer,), daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)


if __name__ == "__main__":
    main()

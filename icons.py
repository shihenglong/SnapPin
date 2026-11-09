"""SVG 线性图标集 — 手绘原始 SVG，仿 PixPin 细线性风格（不提取任何第三方资源）。"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 全部 24x24 viewBox，stroke 风格
_P = {
    "rect":      '<rect x="3.5" y="5.5" width="17" height="13" rx="1.5"/>',
    "ellipse":   '<ellipse cx="12" cy="12" rx="8.5" ry="6.5"/>',
    "line":      '<path d="M4 18 L20 6"/>',
    "arrow":     '<path d="M4 19 L19 5 M19 5 h-6 M19 5 v6"/>',
    "pen":       '<path d="M4 20 l1-4 L16.5 4.5 a1.8 1.8 0 0 1 3 3 L8 19 l-4 1 z M14.5 6.5 l3 3"/>',
    "marker":    '<path d="M9 17 L5 13 l8-8 4 4 -8 8 z M5 13 l-1.5 4.5 L8 16 M13 20 h7"/>',
    "mosaic":    '<path d="M4 4h4v4H4z M8 8h4v4H8z M12 4h4v4h-4z M16 8h4v4h-4z M4 12h4v4H4z M12 12h4v4h-4z M8 16h4v4H8z M16 16h4v4h-4z"/>',
    "text":      '<path d="M5 6 V4.5 h14 V6 M12 4.5 V20 M9 20 h6"/>',
    "eraser":    '<path d="M8.5 20 L3.8 15.3 a1.5 1.5 0 0 1 0-2.1 L13.2 3.8 a1.5 1.5 0 0 1 2.1 0 l4.9 4.9 a1.5 1.5 0 0 1 0 2.1 L11 20 H8.5 z M7 9.5 l7 7 M11 20 h9"/>',
    "undo":      '<path d="M8 5 L3.5 9.5 L8 14 M3.5 9.5 H15 a5.5 5.5 0 0 1 0 11 H9"/>',
    "redo":      '<path d="M16 5 l4.5 4.5 L16 14 M20.5 9.5 H9 a5.5 5.5 0 0 0 0 11 h6"/>',
    "pin":       '<path d="M9 3 v6.5 L5.5 14 v1.5 h13 V14 L15 9.5 V3 M9 3 h6 M12 15.5 V21 M12 15.5 a1.2 1.2 0 0 0 0-.01"/>',
    "gif":       '<rect x="3" y="6" width="18" height="12" rx="2"/>'
                 '<text x="12" y="15.4" font-size="6.5" font-family="Segoe UI" font-weight="600" text-anchor="middle" stroke="none" fill="currentColor">GIF</text>',
    "serial":    '<circle cx="12" cy="12" r="8.5"/>'
                 '<text x="12" y="15.8" font-size="9" font-family="Segoe UI" font-weight="600" text-anchor="middle" stroke="none" fill="currentColor">1</text>',
    "close":     '<path d="M6 6 L18 18 M18 6 L6 18"/>',
    "ocr":       '<path d="M4 8 V5.5 A1.5 1.5 0 0 1 5.5 4 H8 M16 4 h2.5 A1.5 1.5 0 0 1 20 5.5 V8 M20 16 v2.5 a1.5 1.5 0 0 1 -1.5 1.5 H16 M8 20 H5.5 A1.5 1.5 0 0 1 4 18.5 V16 M8 10 v4 M12 9.5 v5 M16 10 v4"/>',
    "save":      '<path d="M12 4 v10 M8 10.5 l4 4 4-4 M5 19.5 h14"/>',
    "copy":      '<rect x="8.5" y="8.5" width="11" height="11" rx="1.5"/>'
                 '<path d="M5.5 15.5 h-1 a1 1 0 0 1 -1-1 v-10 a1 1 0 0 1 1-1 h10 a1 1 0 0 1 1 1 v1"/>',
    "longshot":  '<rect x="4" y="4" width="16" height="13" rx="1.5"/>'
                 '<path d="M12 6.5 v5.5 M9.5 9.5 l2.5 2.5 2.5-2.5"/>'
                 '<path d="M4 20.5 h16" stroke-dasharray="2.5 2.5"/>',
    # 设置导航
    "nav_system":'<path d="M5 7 h9 M17.5 7 H19 M5 12 h2 M10.5 12 H19 M5 17 h9 M17.5 17 H19"/>'
                 '<circle cx="15.7" cy="7" r="1.8"/><circle cx="8.7" cy="12" r="1.8"/><circle cx="15.7" cy="17" r="1.8"/>',
    "nav_look":  '<path d="M12 3.5 a8.5 8.5 0 1 0 0 17 a2.4 2.4 0 0 1 0-4.8 a2.6 2.6 0 0 0 0-5.2 a2.3 2.3 0 0 1 0-4.6 a8.5 8.5 0 0 1 0-2.4 z"/>'
                 '<circle cx="8" cy="9" r="1.2"/><circle cx="7.5" cy="14.5" r="1.2"/><circle cx="11" cy="18" r="1.2"/>',
    "nav_shot":  '<path d="M4 8 V6 a2 2 0 0 1 2-2 h2 M16 4 h2 a2 2 0 0 1 2 2 v2 M20 16 v2 a2 2 0 0 1 -2 2 h-2 M8 20 H6 a2 2 0 0 1 -2-2 v-2"/>'
                 '<rect x="8" y="8" width="8" height="8" rx="1"/>',
    "nav_pin":   '<rect x="4" y="4" width="10" height="12" rx="1.5"/>'
                 '<path d="M17 9 h1.5 a1.5 1.5 0 0 1 1.5 1.5 V19 a1.5 1.5 0 0 1 -1.5 1.5 H11 A1.5 1.5 0 0 1 9.5 19 v-1"/>',
    "nav_hotkey":'<rect x="3" y="7" width="18" height="11" rx="2"/>'
                 '<path d="M6.5 10.5 h.01 M10 10.5 h.01 M13.5 10.5 h.01 M17 10.5 h.01 M6.5 13.5 h.01 M17 13.5 h.01 M9 14.8 h6"/>',
    "nav_clip":  '<rect x="5" y="4.5" width="14" height="16" rx="2"/>'
                 '<path d="M9 4.5 V3.5 h6 v1 M9 9.5 h6 M9 13.5 h6 M9 17 h3.5"/>',
    "nav_pw":    '<circle cx="8" cy="12" r="3.5"/>'
                 '<path d="M11.5 12 H20 M16.5 12 v3 M19.5 12 v2.5"/>',
    "nav_tr":    '<path d="M4 7 V5 h8 v2 M8 5 v8 M5.5 13 h5"/>'
                 '<path d="M13.5 19.5 l3-7 3 7 M14.8 16.8 h3.6"/>',
    "nav_about": '<circle cx="12" cy="12" r="8.5"/>'
                 '<path d="M12 11 v5 M12 7.6 v.01"/>',
}


def icon(name: str, color: str = "#5F6368", size: int = 24, stroke: float = 1.5) -> QIcon:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
           f'stroke-linejoin="round">{_P[name]}</svg>')
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(p)
    p.end()
    return QIcon(QPixmap.fromImage(img))


def pixmap(name: str, color: str = "#5F6368", size: int = 24, stroke: float = 1.5) -> QPixmap:
    return icon(name, color, size, stroke).pixmap(size, size)


APP_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<defs><linearGradient id="appbg" x1="0" y1="0" x2="1" y2="1">'
    '<stop offset="0" stop-color="#FDA4AF"/><stop offset="1" stop-color="#F472B6"/></linearGradient></defs>'
    '<rect x="32" y="32" width="448" height="448" rx="104" fill="url(#appbg)"/>'
    '<rect x="146" y="196" width="220" height="150" rx="20" fill="#FB7185" transform="rotate(-20 256 271)"/>'
    '<rect x="146" y="196" width="220" height="150" rx="20" fill="#F43F5E" transform="rotate(-10 256 271)"/>'
    '<rect x="146" y="196" width="220" height="150" rx="20" fill="#FFF1F2"/>'
    '<rect x="176" y="238" width="110" height="14" rx="7" fill="#FB7185"/>'
    '<rect x="176" y="268" width="150" height="14" rx="7" fill="#FB7185"/>'
    '<rect x="176" y="298" width="88" height="14" rx="7" fill="#FB7185"/>'
    '<rect x="250" y="198" width="12" height="52" rx="6" fill="#9F1239"/>'
    '<circle cx="256" cy="192" r="26" fill="#FBBF24"/>'
    '<circle cx="246" cy="182" r="8" fill="#FDE68A"/></svg>')

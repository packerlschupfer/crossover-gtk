# Maintainer: packerlschupfer
pkgname=crossover-gtk
pkgver=0.1.3
pkgrel=1
pkgdesc="Lightweight crosshair overlay for Linux gaming"
arch=('any')
url="https://github.com/packerlschupfer/crossover-gtk"
license=('GPL-3.0-or-later')
depends=(
    'python'
    'python-gobject'
    'gtk3'
    'python-cairo'
)
optdepends=(
    'libayatana-appindicator: system tray icon'
    'gtk-layer-shell: Wayland support'
)
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
    cd "$srcdir/$pkgname-$pkgver"
    install -Dm755 crossover.py "$pkgdir/usr/share/$pkgname/crossover.py"
    install -Dm755 /dev/stdin "$pkgdir/usr/bin/crossover-gtk" <<EOF
#!/bin/sh
exec python3 /usr/share/$pkgname/crossover.py "\$@"
EOF
    install -Dm644 crossover-gtk.desktop "$pkgdir/usr/share/applications/crossover-gtk.desktop"
    install -Dm644 data/crossover-gtk.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/crossover-gtk.svg"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}

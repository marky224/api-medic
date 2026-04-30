// One-shot builder for Chrome Web Store / AMO listing assets.
// Run with: node store-assets/.build.js  (sharp must resolve from /tmp/svg-convert/node_modules)
const path = require('path');
const fs = require('fs');

const sharpPath = 'C:/Users/me/AppData/Local/Temp/svg-convert/node_modules/sharp';
const sharp = require(sharpPath);

const repoRoot = path.resolve(__dirname, '..');
const out = path.join(repoRoot, 'store-assets');
const shotsOut = path.join(out, 'screenshots');
const imagesIn = path.join(repoRoot, 'images');
const tileSvgPath = path.join(repoRoot, 'docs', 'marketing', 'api-medic-chrome-tile-440x280.svg');

// Brand colors derived from the promo tile SVG.
const TEAL = '#0F3F3D';
const OFFWHITE = '#F4F3EE';

async function buildIcon() {
  // 128x128 with 16px transparent padding -> 96x96 dark-teal inner square,
  // "am" monogram in monospace, off-white. Square corners to match the tile.
  const iconSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
    <rect x="16" y="16" width="96" height="96" fill="${TEAL}"/>
    <text x="64" y="64"
          font-family="'JetBrains Mono', 'IBM Plex Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
          font-size="52"
          font-weight="600"
          fill="${OFFWHITE}"
          text-anchor="middle"
          dominant-baseline="central"
          letter-spacing="-0.02em">am</text>
  </svg>`;
  const dest = path.join(out, 'store-icon-128.png');
  await sharp(Buffer.from(iconSvg))
    .resize(128, 128)
    .png({ compressionLevel: 9 })
    .toFile(dest);
  return dest;
}

async function buildPromoTile() {
  const dest = path.join(out, 'promo-tile-440x280.png');
  // Flatten on the tile's actual background color so the result has no alpha channel.
  await sharp(tileSvgPath)
    .resize(440, 280)
    .flatten({ background: TEAL })
    .png({ palette: false, compressionLevel: 9 })
    .toFile(dest);
  return dest;
}

async function buildScreenshot(filename) {
  const src = path.join(imagesIn, filename);
  const dest = path.join(shotsOut, filename);
  // Aspect-preserving fit into 1280x800 with white padding; flatten ensures 24-bit JPEG.
  await sharp(src)
    .resize(1280, 800, {
      fit: 'contain',
      background: { r: 255, g: 255, b: 255, alpha: 1 },
    })
    .flatten({ background: '#FFFFFF' })
    .jpeg({ quality: 95, chromaSubsampling: '4:4:4', mozjpeg: false })
    .toFile(dest);
  return dest;
}

async function describe(filePath) {
  const m = await sharp(filePath).metadata();
  return {
    file: path.relative(repoRoot, filePath).replace(/\\/g, '/'),
    width: m.width,
    height: m.height,
    format: m.format,
    channels: m.channels,
    hasAlpha: m.hasAlpha,
  };
}

(async () => {
  const results = [];

  results.push(await describe(await buildIcon()));
  results.push(await describe(await buildPromoTile()));

  const shots = [
    '01-hero-failing-request-analyzed.jpg',
    '02-devtools-panel-with-network.jpg',
    '03-healthy-200-response.jpg',
    '04-captured-requests-list.jpg',
    '05-empty-state-getting-started.jpg',
  ];
  for (const f of shots) {
    results.push(await describe(await buildScreenshot(f)));
  }

  // Print verification table.
  const fmt = (s, w) => String(s).padEnd(w);
  console.log(
    fmt('ASSET', 60) +
      fmt('DIMENSIONS', 14) +
      fmt('FORMAT', 10) +
      fmt('ALPHA', 8) +
      'STATUS',
  );
  let allOk = true;
  for (const r of results) {
    let expectAlpha = false;
    let dimsOk = false;
    let formatOk = false;
    if (r.file.endsWith('store-icon-128.png')) {
      expectAlpha = true;
      dimsOk = r.width === 128 && r.height === 128;
      formatOk = r.format === 'png';
    } else if (r.file.endsWith('promo-tile-440x280.png')) {
      expectAlpha = false;
      dimsOk = r.width === 440 && r.height === 280;
      formatOk = r.format === 'png';
    } else {
      expectAlpha = false;
      dimsOk = r.width === 1280 && r.height === 800;
      formatOk = r.format === 'jpeg';
    }
    const alphaOk = r.hasAlpha === expectAlpha;
    const ok = dimsOk && formatOk && alphaOk;
    if (!ok) allOk = false;
    let note = ok ? 'OK' : 'FAIL';
    if (ok && r.file.endsWith('store-icon-128.png')) note = 'OK (alpha allowed for icon)';
    console.log(
      fmt(r.file, 60) +
        fmt(`${r.width}x${r.height}`, 14) +
        fmt(r.format.toUpperCase(), 10) +
        fmt(r.hasAlpha ? 'yes' : 'no', 8) +
        note,
    );
  }
  if (!allOk) {
    process.exitCode = 1;
  }
})();

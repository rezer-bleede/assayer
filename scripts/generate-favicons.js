const fs = require('fs');
const zlib = require('zlib');
const path = require('path');

// Simple PNG encoder - creates valid PNG files from SVG-like drawing commands
// We'll use a minimal approach: create a PNG from SVG by rendering via a simple
// pixel buffer approach. Since we don't have canvas, we'll create PNGs by
// rendering the SVG to a pixel buffer manually.

// Actually, let's use a simpler approach: create PNGs programmatically
// by drawing shapes directly to a buffer

function createPNG(width, height, drawFn) {
  const widthBytes = Math.ceil(width / 1) * 1;
  const heightPx = height;
  
  // Create RGBA buffer
  const buf = Buffer.alloc(width * height * 4, 0);
  
  // Draw using the provided function
  drawFn(buf, width, height);
  
  // PNG encoding
  const png = encodePNG(buf, width, height);
  return png;
}

function encodePNG(imgData, width, height) {
  const bytes = imgData;
  const widthB = width;
  const heightB = height;
  
  const rawData = Buffer.alloc(width * height * 4 + height);
  for (let y = 0; y < height; y++) {
    rawData[y * (width * 4 + 1)] = 0; // filter byte = 0 (none)
    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * 4;
      rawData[y * (width * 4 + 1) + 1 + x * 4] = bytes[idx];
      rawData[y * (width * 4 + 1) + 2 + x * 4] = bytes[idx + 1];
      rawData[y * (width * 4 + 1) + 3 + x * 4] = bytes[idx + 2];
      rawData[y * (width * 4 + 1) + 4 + x * 4] = bytes[idx + 3];
    }
  }
  
  // PNG chunks
  const chunks = [];
  
  // IHDR
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;  // bit depth
  ihdr[9] = 6;  // color type: RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filter
  ihdr[12] = 0; // interlace
  chunks.push(makeChunk('IHDR', ihdr));
  
  // IDAT
  const idatData = zlib.deflateSync(rawData);
  chunks.push(makeChunk('IDAT', idatData));
  
  // IEND
  chunks.push(makeChunk('IEND', Buffer.alloc(0)));
  
  return Buffer.concat(chunks);
}

function makeChunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type);
  const crc = Buffer.alloc(4);
  const crcInput = Buffer.concat([typeBuf, data]);
  const crcVal = crc32(crcInput) >>> 0;
  crc.writeUInt32BE(crc >>> 0, 0);
  return Buffer.concat([len, typeBuf, data, crc]);
}

// Simple CRC32 implementation
function crc32(buf) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) {
    crc ^= buf[i];
    for (let j = 0; j < 8; j++) {
      if (crc & 1) {
        crc = (crc >>> 1) ^ 0xEDB88320;
      } else {
        crc = crc >>> 1;
      }
    }
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

module.exports = { createPNG };

// If run directly, generate the favicon set
if (require.main === module) {
  const outDir = process.argv[2] || './web/public';
  
  // We need to actually draw something. Since we can't easily render SVG,
  // let's create simple colored PNGs that match our design.
  
  const bgColor = [15, 23, 42, 255];      // #0f172a slate-900
  const accentColor = [56, 189, 245, 255]; // #38bdf8 sky-400
  const accentGlow = [34, 211, 238, 255]; // #22d3ee cyan-400
  const textLight = [226, 232, 240, 255]; // #e2e8f0 slate-200
  const textMid = [148, 163, 244, 255];   // #94a3b8 slate-400
  
  function drawLogo(buf, w, h) {
    // Fill background
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = (y * w + x) * 4;
        buf[i] = bgColor[0];
        buf[i + 1] = bgColor[1];
        buf[i + 2] = bgColor[2];
        buf[i + 3] = bgColor[3];
      }
    }
    
    // Rounded rect bg (simpler: just fill center)
    const cx = w / 2, cy = h / 2, r = h * 0.45;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const dx = x - cx, dy = y - cy;
        if (dx * dx + dy * dy <= r * r) {
          const i = (y * w + x) * 4;
          buf[i] = 15; buf[i + 1] = 23; buf[i + 2] = 42; buf[i + 3] = 255;
        }
      }
    }
    
    // Draw a Q shape
    // Outer ring
    const cx2 = w / 2, cy2 = h / 2;
    const rOuter = h * 0.28, rInner = h * 0.20;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const dx = x - cx2, dy = y - cy2;
        const d2 = dx * dx + dy * dy;
        if (d2 <= rOuter * rOuter && d2 >= rInner * rInner) {
          const i = (y * w + x) * 4;
          buf[i] = 56; buf[i + 1] = 189; buf[i + 2] = 245; buf[i + 3] = 255;
        }
      }
    }
    
    // Tail of Q
    for (let y = cy2 + rOuter * 0.6; y < cy2 + rOuter * 0.9; y++) {
      for (let x = cx2 + rOuter * 0.4; x < cx2 + rOuter * 0.8; x++) {
        if (x >= 0 && x < w && y >= 0 && y < h) {
          const i = (y * w + x) * 4;
          buf[i] = 56; buf[i + 1] = 189; buf[i + 2] = 245; buf[i + 3] = 255;
        }
      }
    }
    
    // Checkmark
    const cx3 = w * 0.62, cy3 = h * 0.62;
    const checkPoints = [
      [cx3 - rOuter * 0.15, cy2 + rOuter * 0.15],
      [cx2 + rOuter * 0.05, cy2 + rOuter * 0.45],
      [cx2 + rOuter * 0.55, cy2 - rOuter * 0.15]
    ];
    for (let i = 0; i < checkPoints.length - 1; i++) {
      const [x1, y1] = checkPoints[i];
      const [x2, y2] = checkPoints[i + 1];
      const dx = x2 - x1, dy = y2 - y1;
      const len = Math.sqrt(dx * dx + dy * dy);
      const steps = Math.max(len, 1);
      for (let s = 0; s <= steps; s++) {
        const t = s / steps;
        const x = x1 + dx * t, y = y1 + dy * t;
        const ix = Math.round(x), iy = Math.round(y);
        if (ix >= 0 && ix < w && iy >= 0 && iy < h) {
          const pi = (iy * w + ix) * 4;
          buf[pi] = 34; buf[pi + 1] = 211; buf[pi + 2] = 238; buf[pi + 3] = 255;
        }
      }
    }
    
    // Draw data table rows on left side of Q
    const rowY = [cy2 - rOuter * 0.5, cy2 - rOuter * 0.2, cy2 + rOuter * 0.1];
    const rowX = cx2 - rOuter * 0.6;
    const rowW = rOuter * 0.9;
    for (const ry of rowY) {
      for (let x = rowX; x < rowX + rowW; x++) {
        for (let y = ry - 1; y <= ry + 1; y++) {
          if (x >= 0 && x < w && y >= 0 && y < h) {
            const i = (y * w + x) * 4;
            buf[i] = 226; buf[i + 1] = 232; buf[i + 2] = 240; buf[i + 3] = 255;
          }
        }
      }
    }
  }
  
  // Generate favicon sizes
  const sizes = [16, 32, 48, 64, 128, 256, 512];
  for (const size of sizes) {
    const png = createPNG(size, size, drawLogo);
    fs.writeFileSync(path.join(outDir, `favicon-${size}x${size}.png`), png);
    console.log(`Created favicon-${size}x${size}.png (${png.length} bytes)`);
  }
  
  // Create ICO from multiple sizes
  const icoSizes = [16, 32, 48];
  const icoDirs = [];
  const icoImages = [];
  for (const size of icoSizes) {
    const png = createPNG(size, size, drawLogo);
    icoImages.push(png);
    icoDirs.push({ width: size, height: size, offset: 0 });
  }
  
  let offset = 6 + icoDirs.length * 16;
  const icoHeader = Buffer.alloc(6);
  icoHeader.writeUInt16LE(0, 0); // reserved
  icoHeader.writeUInt16LE(1, 2); // type = 1 (ICO)
  icoHeader.writeUInt16LE(icoDirs.length, 2); // count
  
  let dirData = Buffer.alloc(icoDirs.length * 16);
  let imageData = Buffer.alloc(0);
  
  for (let i = 0; i < icoDirs.length; i++) {
    const img = icoImages[i];
    dirData.writeUInt8(icoDirs[i].width, i * 16 + 0);   // width
    dirData.writeUInt8(icoDirs[i].height, i * 16 + 1);  // height
    dirData.writeUInt8(0, i * 16 + 2);                   // color count
    dirData.writeUInt8(0, i * 16 + 3);                   // reserved
    dirData.writeUInt16LE(1, i * 16 + 4);                // color planes
    dirData.writeUInt16LE(32, i * 16 + 6);               // bits per pixel
    dirData.writeUInt32LE(img.length, i * 16 + 8);       // size
    dirData.writeUInt32LE(offset, i * 16 + 12);          // offset
    offset += img.length;
    imageData = Buffer.concat([imageData, img]);
  }
  
  const ico = Buffer.concat([icoHeader, dirData, imageData]);
  fs.writeFileSync(path.join(outDir, 'favicon.ico'), ico);
  console.log(`Created favicon.ico (${ico.length} bytes)`);
  
  // Create apple-touch-icon (180x180)
  const applePNG = createPNG(180, 180, drawLogo);
  fs.writeFileSync(path.join(outDir, 'apple-touch-icon.png'), applePNG);
  console.log(`Created apple-touch-icon.png (${applePNG.length} bytes)`);
  
  // Create android chrome icons
  const android192 = createPNG(192, 192, drawLogo);
  fs.writeFileSync(path.join(outDir, 'android-chrome-192x192.png'), android192);
  console.log(`Created android-chrome-192x192.png (${android192.length} bytes)`);
  
  const android512 = createPNG(512, 512, drawLogo);
  fs.writeFileSync(path.join(outDir, 'android-chrome-512x512.png'), android512);
  console.log(`Created android-chrome-512x512.png (${android512.length} bytes)`);
  
  console.log('\nAll favicon assets generated!');
}

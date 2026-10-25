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
  
  // Signature: every PNG must start with these 8 bytes or readers reject it.
  chunks.push(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  
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
  crc.writeUInt32BE(crcVal, 0);
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
    // Draw at 4x then downsample so the transparent mark stays crisp at 16px.
    const scale = 4, unit = (w / 64) * scale, sw = w * scale, sh = h * scale;
    const high = Buffer.alloc(sw * sh * 4);
    const mark = [56, 189, 248, 255], check = [34, 211, 238, 255];
    const muted = [148, 163, 184, 255], outline = [15, 23, 42, 255];
    const paint = (x, y, color) => {
      const i = (y * sw + x) * 4;
      high[i] = color[0]; high[i + 1] = color[1]; high[i + 2] = color[2]; high[i + 3] = color[3];
    };
    const line = (x1, y1, x2, y2, width, color) => {
      x1 *= unit; y1 *= unit; x2 *= unit; y2 *= unit; width *= unit;
      const dx = x2 - x1, dy = y2 - y1, len2 = dx * dx + dy * dy;
      for (let y = Math.max(0, Math.floor(Math.min(y1, y2) - width)); y < Math.min(sh, Math.ceil(Math.max(y1, y2) + width)); y++)
        for (let x = Math.max(0, Math.floor(Math.min(x1, x2) - width)); x < Math.min(sw, Math.ceil(Math.max(x1, x2) + width)); x++) {
          const t = Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / len2));
          if (Math.hypot(x - x1 - t * dx, y - y1 - t * dy) <= width / 2) paint(x, y, color);
        }
    };
    const circle = (cx, cy, radius, width, color) => {
      cx *= unit; cy *= unit; radius *= unit; width *= unit;
      for (let y = Math.max(0, Math.floor(cy - radius - width)); y < Math.min(sh, Math.ceil(cy + radius + width)); y++)
        for (let x = Math.max(0, Math.floor(cx - radius - width)); x < Math.min(sw, Math.ceil(cx + radius + width)); x++)
          if (Math.abs(Math.hypot(x - cx, y - cy) - radius) <= width / 2) paint(x, y, color);
    };
    [[10,22,22,22],[10,32,18,32],[10,42,22,42]].forEach(([x1,y1,x2,y2]) => line(x1,y1,x2,y2,4,muted));
    circle(37, 29, 17, 8, outline); circle(37, 29, 17, 5, mark);
    line(30,29,35,34,7,outline); line(35,34,44,24,7,outline);
    line(30,29,35,34,4,check); line(35,34,44,24,4,check);
    line(49,42,58,52,9,outline); line(49,42,58,52,5,mark);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const sum = [0,0,0,0];
      for (let sy = 0; sy < scale; sy++) for (let sx = 0; sx < scale; sx++) {
        const i = (((y * scale + sy) * sw) + x * scale + sx) * 4;
        for (let c = 0; c < 4; c++) sum[c] += high[i + c];
      }
      const out = (y * w + x) * 4;
      for (let c = 0; c < 4; c++) buf[out + c] = Math.round(sum[c] / (scale * scale));
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
  icoHeader.writeUInt16LE(icoDirs.length, 4); // count
  
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

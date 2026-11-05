// Standalone XHS Lite Worker extracted from SullyOS worker/index.js
// Source: worker/index.js cors/json helpers + XHSLite module + /api/<command> route
// Deploy on Cloudflare Workers or any Worker-compatible runtime with fetch, crypto.subtle, TextEncoder.

function corsHeaders(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Depth, X-Brave-API-Key, X-Notion-API-Key, X-Feishu-Token, X-Xhs-Cookie, X-Netease-Cookie, X-WebDAV-Method, X-WebDAV-Depth, X-WebDAV-Range, X-GitHub-Method, X-GitHub-Api-Version, Mcp-Session-Id, Accept, Range",
    "Access-Control-Expose-Headers": "Mcp-Session-Id",
    "Access-Control-Max-Age": "86400",
  };
}

function jsonResponse(obj, { status = 200, origin } = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...corsHeaders(origin),
    },
  });
}

const XHSLite = (() => {
  const STANDARD_B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const CUSTOM_B64 = 'ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5';
  const X3_B64 = 'MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G';
  const HEX_KEY =
    '71a302257793271ddd273bcee3e4b98d9d7935e1da33f5765e2ea8afb6dc77a5' +
    '1a499d23b67c20660025860cbf13d4540d92497f58686c574e508f46e1956344' +
    'f39139bf4faf22a3eef120b79258145b2feb5193b6478669961298e79bedca64' +
    '6e1a693a926154a5a7a1bd1cf0dedb742f917a747a1e388b234f2277516db711' +
    '6035439730fa61e9822a0eca7bff72d8';
  const VERSION_BYTES = [121, 104, 96, 41];
  const PAYLOAD_LENGTH = 144, A1_LENGTH = 52, APP_ID_LENGTH = 10;
  const A3_PREFIX = [2, 97, 51, 16];
  const ENV_TABLE = [115, 248, 83, 102, 103, 201, 181, 131, 99, 94, 4, 68, 250, 132, 21];
  const ENV_CHECKS_DEFAULT = [0, 1, 18, 1, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0];
  const HASH_IV = [1831565813, 461845907, 2246822507, 3266489909];
  const X3_PREFIX = 'mns0301_', XYS_PREFIX = 'XYS_', B1_SECRET_KEY = 'xhswebmplfbt';
  const SIGNATURE_DATA_TEMPLATE = { x0: '4.2.6', x1: 'xhs-pc-web', x2: 'Windows', x3: '', x4: '' };
  const SIGNATURE_XSCOMMON_TEMPLATE = {
    s0: 5, s1: '', x0: '1', x1: '4.2.6', x2: 'Windows', x3: 'xhs-pc-web', x4: '4.86.0',
    x5: '', x6: '', x7: '', x8: '', x9: -596800761, x10: 0, x11: 'normal',
  };
  const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0';
  const IMG_FORMATS = ['jpg', 'webp', 'avif'];
  const EDITH = 'https://edith.xiaohongshu.com', CREATOR = 'https://creator.xiaohongshu.com', WWW = 'https://www.xiaohongshu.com';

  const RNG = {
    randint(a, b) { return a + Math.floor(Math.random() * (b - a + 1)); },
    randbytes(n) { const o = new Uint8Array(n); crypto.getRandomValues(o); return o; },
  };

  const u32 = (v) => v >>> 0;
  const rotl = (v, n) => u32((v << n) | (v >>> (32 - n)));
  const utf8 = (s) => new TextEncoder().encode(s);
  function intToLeBytes(val, length = 4) {
    const arr = []; let v = val;
    for (let i = 0; i < length; i++) { arr.push(v & 0xff); v = Math.floor(v / 256); }
    return arr;
  }
  function hexToBytes(hex) {
    const out = [];
    for (let i = 0; i < hex.length; i += 2) out.push(parseInt(hex.slice(i, i + 2), 16));
    return out;
  }

  function md5Hex(bytes) {
    if (typeof bytes === 'string') bytes = utf8(bytes);
    const s = [7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
      5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
      4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
      6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21];
    const K = [];
    for (let i = 0; i < 64; i++) K[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) >>> 0;
    const ml = bytes.length * 8;
    const withOne = bytes.length + 1;
    const padLen = ((withOne + 8 + 63) & ~63) - withOne - 8;
    const total = bytes.length + 1 + padLen + 8;
    const msg = new Uint8Array(total);
    msg.set(bytes); msg[bytes.length] = 0x80;
    const lenLo = ml >>> 0, lenHi = Math.floor(ml / 4294967296) >>> 0;
    for (let i = 0; i < 4; i++) msg[total - 8 + i] = (lenLo >>> (8 * i)) & 0xff;
    for (let i = 0; i < 4; i++) msg[total - 4 + i] = (lenHi >>> (8 * i)) & 0xff;
    let a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    for (let off = 0; off < total; off += 64) {
      const M = new Array(16);
      for (let i = 0; i < 16; i++) {
        M[i] = ((msg[off + i * 4]) | (msg[off + i * 4 + 1] << 8) | (msg[off + i * 4 + 2] << 16) | (msg[off + i * 4 + 3] << 24)) >>> 0;
      }
      let A = a0, B = b0, C = c0, D = d0;
      for (let i = 0; i < 64; i++) {
        let F, g;
        if (i < 16) { F = (B & C) | (~B & D); g = i; }
        else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16; }
        else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16; }
        else { F = C ^ (B | (~D >>> 0)); g = (7 * i) % 16; }
        F = (F + A + K[i] + M[g]) >>> 0;
        A = D; D = C; C = B; B = (B + rotl(F, s[i])) >>> 0;
      }
      a0 = (a0 + A) >>> 0; b0 = (b0 + B) >>> 0; c0 = (c0 + C) >>> 0; d0 = (d0 + D) >>> 0;
    }
    const toHex = (n) => { let h = ''; for (let i = 0; i < 4; i++) h += ((n >>> (8 * i)) & 0xff).toString(16).padStart(2, '0'); return h; };
    return toHex(a0) + toHex(b0) + toHex(c0) + toHex(d0);
  }

  function bytesToStdB64(bytes) {
    let out = ''; const n = bytes.length;
    for (let i = 0; i < n; i += 3) {
      const b0 = bytes[i], b1 = i + 1 < n ? bytes[i + 1] : 0, b2 = i + 2 < n ? bytes[i + 2] : 0;
      out += STANDARD_B64[b0 >> 2];
      out += STANDARD_B64[((b0 & 3) << 4) | (b1 >> 4)];
      out += i + 1 < n ? STANDARD_B64[((b1 & 15) << 2) | (b2 >> 6)] : '=';
      out += i + 2 < n ? STANDARD_B64[b2 & 63] : '=';
    }
    return out;
  }
  function translateAlphabet(str, to) {
    let out = '';
    for (const ch of str) { const idx = STANDARD_B64.indexOf(ch); out += idx === -1 ? ch : to[idx]; }
    return out;
  }
  const encodeCustom = (bytes) => translateAlphabet(bytesToStdB64(bytes), CUSTOM_B64);
  const encodeX3 = (bytes) => translateAlphabet(bytesToStdB64(bytes), X3_B64);
  const encodeCustomStr = (str) => encodeCustom(utf8(str));

  const CRC_POLY = 0xedb88320;
  let CRC_TABLE = null;
  function crcTable() {
    if (CRC_TABLE) return CRC_TABLE;
    CRC_TABLE = new Uint32Array(256);
    for (let d = 0; d < 256; d++) { let r = d; for (let k = 0; k < 8; k++) r = (r & 1) ? ((r >>> 1) ^ CRC_POLY) : (r >>> 1); CRC_TABLE[d] = r >>> 0; }
    return CRC_TABLE;
  }
  function crc32JsInt(str) {
    const tbl = crcTable(); let c = 0xffffffff;
    for (let i = 0; i < str.length; i++) { const b = str.charCodeAt(i) & 0xff; c = (tbl[(c ^ b) & 0xff] ^ (c >>> 8)) >>> 0; }
    const v = ((0xffffffff ^ c) ^ CRC_POLY) >>> 0;
    return v & 0x80000000 ? v - 0x100000000 : v;
  }

  function rc4(keyBytes, dataBytes) {
    const S = new Uint8Array(256);
    for (let i = 0; i < 256; i++) S[i] = i;
    let j = 0;
    for (let i = 0; i < 256; i++) { j = (j + S[i] + keyBytes[i % keyBytes.length]) & 0xff; const t = S[i]; S[i] = S[j]; S[j] = t; }
    const out = new Uint8Array(dataBytes.length);
    let a = 0, b = 0;
    for (let k = 0; k < dataBytes.length; k++) {
      a = (a + 1) & 0xff; b = (b + S[a]) & 0xff;
      const t = S[a]; S[a] = S[b]; S[b] = t;
      out[k] = dataBytes[k] ^ S[(S[a] + S[b]) & 0xff];
    }
    return out;
  }

  function customHashV2(inputBytes) {
    let [s0, s1, s2, s3] = HASH_IV;
    const length = inputBytes.length;
    s0 = u32(s0 ^ length); s1 = u32(s1 ^ u32(length << 8)); s2 = u32(s2 ^ u32(length << 16)); s3 = u32(s3 ^ u32(length << 24));
    const dv = new DataView(new Uint8Array(inputBytes).buffer);
    for (let i = 0; i < Math.floor(length / 8); i++) {
      const v0 = dv.getUint32(i * 8, true), v1 = dv.getUint32(i * 8 + 4, true);
      s0 = rotl(u32(u32(s0 + v0) ^ s2), 7);
      s1 = rotl(u32(u32(v0 ^ s1) + s3), 11);
      s2 = rotl(u32(u32(s2 + v1) ^ s0), 13);
      s3 = rotl(u32(u32(s3 ^ v1) + s1), 17);
    }
    const t0 = u32(s0 ^ length), t1 = u32(s1 ^ t0), t2 = u32(s2 + t1), t3 = u32(s3 ^ t2);
    const r0 = rotl(t0, 9), r1 = rotl(t1, 13), r2 = rotl(t2, 17), r3 = rotl(t3, 19);
    s0 = u32(r0 + r2); s1 = u32(r1 ^ r3); s2 = u32(r2 + s0); s3 = u32(r3 ^ s1);
    const result = [];
    for (const s of [s0, s1, s2, s3]) result.push(...intToLeBytes(s, 4));
    return result;
  }

  function pyQuote(value, safeExtra) {
    const keep = new Set();
    const always = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~';
    for (const c of always) keep.add(c);
    for (const c of safeExtra) keep.add(c);
    let out = '';
    for (const byte of utf8(value)) {
      const ch = String.fromCharCode(byte);
      if (byte < 0x80 && keep.has(ch)) out += ch;
      else out += '%' + byte.toString(16).toUpperCase().padStart(2, '0');
    }
    return out;
  }
  const jsonCompact = (obj) => JSON.stringify(obj);
  function buildContentString(method, uri, payload) {
    payload = payload || {};
    if (method.toUpperCase() === 'POST') return uri + jsonCompact(payload);
    const keys = Object.keys(payload);
    if (!keys.length) return uri;
    const parts = keys.map((k) => {
      const v = payload[k];
      let s; if (Array.isArray(v)) s = v.map(String).join(','); else if (v !== null && v !== undefined) s = String(v); else s = '';
      return `${k}=${pyQuote(s, ',')}`;
    });
    return `${uri}?${parts.join('&')}`;
  }
  function extractApiPath(s) {
    const brace = s.indexOf('{'), q = s.indexOf('?');
    if (brace !== -1 && q !== -1) return s.slice(0, Math.min(brace, q));
    if (brace !== -1) return s.slice(0, brace);
    if (q !== -1) return s.slice(0, q);
    return s;
  }
  function extractUri(uri) {
    uri = uri.trim();
    if (uri.startsWith('http')) return new URL(uri).pathname;
    const q = uri.indexOf('?');
    return q === -1 ? uri : uri.slice(0, q);
  }

  function buildPayloadArray(dValue, a1Value, appId, stringParam, timestampSec) {
    const seed = RNG.randint(0, 0xffffffff), seedByte = seed & 0xff;
    const payload = [...VERSION_BYTES];
    payload.push(...intToLeBytes(seed, 4));
    const tsMs = Math.floor(timestampSec * 1000), tsBytes = intToLeBytes(tsMs, 8);
    payload.push(...tsBytes);
    const timeOffset = RNG.randint(10, 50);
    payload.push(...intToLeBytes(Math.floor((timestampSec - timeOffset) * 1000), 8));
    payload.push(...intToLeBytes(RNG.randint(15, 50), 4));
    payload.push(...intToLeBytes(RNG.randint(1000, 1200), 4));
    payload.push(...intToLeBytes(utf8(stringParam).length, 4));
    const md5Bytes = hexToBytes(dValue);
    for (let i = 0; i < 8; i++) payload.push(md5Bytes[i] ^ seedByte);
    const a1Full = utf8(a1Value).slice(0, A1_LENGTH);
    const a1Bytes = new Uint8Array(A1_LENGTH); a1Bytes.set(a1Full);
    payload.push(a1Bytes.length); payload.push(...a1Bytes);
    const appFull = utf8(appId).slice(0, APP_ID_LENGTH);
    const appBytes = new Uint8Array(APP_ID_LENGTH); appBytes.set(appFull);
    payload.push(appBytes.length); payload.push(...appBytes);
    const part11 = [1, seedByte ^ ENV_TABLE[0]];
    for (let i = 1; i < 15; i++) part11.push(ENV_TABLE[i] ^ ENV_CHECKS_DEFAULT[i]);
    payload.push(...part11);
    const md5PathBytes = hexToBytes(md5Hex(extractApiPath(stringParam)));
    const hashed = customHashV2([...tsBytes, ...md5PathBytes]);
    payload.push(...A3_PREFIX, ...hashed.map((b) => b ^ seedByte));
    return payload;
  }
  function xorTransform(src) {
    const key = hexToBytes(HEX_KEY);
    const out = new Uint8Array(src.length);
    for (let i = 0; i < src.length; i++) out[i] = (i < key.length ? (src[i] ^ key[i]) : src[i]) & 0xff;
    return out;
  }

  function signXs(method, uri, a1Value, { appId = 'xhs-pc-web', payload = null, timestampSec = null } = {}) {
    uri = extractUri(uri);
    if (timestampSec === null) timestampSec = Date.now() / 1000;
    const contentString = buildContentString(method, uri, payload);
    const dValue = md5Hex(contentString);
    const xorResult = xorTransform(buildPayloadArray(dValue, a1Value, appId, contentString, timestampSec));
    const x3sig = encodeX3(xorResult.slice(0, PAYLOAD_LENGTH));
    return XYS_PREFIX + encodeCustomStr(jsonCompact({ ...SIGNATURE_DATA_TEMPLATE, x3: X3_PREFIX + x3sig }));
  }

  function generateB1(fp) {
    const keys = ['x33', 'x34', 'x35', 'x36', 'x37', 'x38', 'x39', 'x42', 'x43', 'x44', 'x45', 'x46', 'x48', 'x49', 'x50', 'x51', 'x52', 'x82'];
    const b1fp = {};
    for (const k of keys) b1fp[k] = fp[k];
    const cipher = rc4(utf8(B1_SECRET_KEY), utf8(jsonCompact(b1fp)));
    let cipherStr = '';
    for (const b of cipher) cipherStr += String.fromCharCode(b);
    const encodedUrl = pyQuote(cipherStr, "!*'()~_-");
    const b = [];
    for (const c of encodedUrl.split('%').slice(1)) {
      b.push(parseInt(c.slice(0, 2), 16));
      for (const ch of c.slice(2)) b.push(ch.charCodeAt(0));
    }
    return encodeCustom(b);
  }

  const GPU_VENDORS = [
    'Google Inc. (Intel)|ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E9B) Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'Google Inc. (NVIDIA)|ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x0000250F) Direct3D11 vs_5_0 ps_5_0, D3D11)',
    'Google Inc. (AMD)|ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)',
  ];
  const SCREEN_RES = ['1366;768', '1920;1080', '2560;1440'];
  const pick = (arr) => arr[RNG.randint(0, arr.length - 1)];
  const randMd5 = () => md5Hex(RNG.randbytes(32));
  function generateFingerprint(cookies) {
    const cookieString = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
    const [w, h] = pick(SCREEN_RES).split(';').map(Number);
    const incognito = RNG.randint(0, 99) < 95 ? 'true' : 'false';
    const [vendor, renderer] = pick(GPU_VENDORS).split('|');
    return {
      x1: UA, x2: 'false', x3: 'zh-CN', x4: pick([16, 24, 30, 32]), x5: pick([2, 4, 8, 16]), x6: '24',
      x7: `${vendor},${renderer}`, x8: pick([4, 6, 8, 12, 16]), x9: `${w};${h}`, x10: `${w};${h}`, x11: '-480', x12: 'Asia/Shanghai',
      x13: incognito, x14: incognito, x15: incognito, x16: 'false', x17: 'false', x18: 'un', x19: 'Win32', x20: '',
      x21: 'PDF Viewer,Chrome PDF Viewer', x22: randMd5(), x23: 'false', x24: 'false', x25: 'false', x26: 'false', x27: 'false',
      x28: '0,false,false', x29: '4,7,8', x30: 'swf object not loaded',
      x33: '0', x34: '0', x35: '0', x36: `${RNG.randint(1, 20)}`,
      x37: '0|0|0|0|0|0|0|0|0|1|0|0|0|0|0|0|0|0|1|0|0|0|0|0',
      x38: '0|0|1|0|1|0|0|0|0|0|1|0|1|0|1|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0',
      x39: 0, x40: '0', x41: '0', x42: '3.4.4', x43: randMd5(), x44: `${Date.now()}`,
      x45: '__SEC_CAV__1-1-1-1-1|__SEC_WSA__|', x46: 'false', x47: '1|0|0|0|0|0',
      x48: '', x49: '{list:[],type:}', x50: '', x51: '', x52: '', x82: '_0x17a2|_0x1954',
      x53: randMd5(), x57: cookieString,
    };
  }

  function signXsCommon(cookieDict, fingerprint) {
    const fp = fingerprint || generateFingerprint(cookieDict);
    const b1 = generateB1(fp);
    return encodeCustomStr(jsonCompact({ ...SIGNATURE_XSCOMMON_TEMPLATE, x5: cookieDict.a1, x8: b1, x9: crc32JsInt(b1) }));
  }

  const HEX_CHARS = 'abcdef0123456789';
  function b3TraceId() { let s = ''; for (let i = 0; i < 16; i++) s += HEX_CHARS[RNG.randint(0, 15)]; return s; }
  function xrayTraceId(tsMs) {
    if (!tsMs) tsMs = Date.now();
    const part1 = ((BigInt(tsMs) << 23n) | BigInt(RNG.randint(0, 8388607))).toString(16).padStart(16, '0');
    let part2 = ''; for (let i = 0; i < 16; i++) part2 += HEX_CHARS[RNG.randint(0, 15)];
    return part1 + part2;
  }
  function signHeaders(method, uri, cookieDict, { params = null, payload = null, timestampSec = null } = {}) {
    if (timestampSec === null) timestampSec = Date.now() / 1000;
    const m = method.toUpperCase();
    const requestData = m === 'GET' ? params : payload;
    return {
      'x-s': signXs(m, uri, cookieDict.a1, { payload: requestData, timestampSec }),
      'x-s-common': signXsCommon(cookieDict),
      'x-t': String(Math.floor(timestampSec * 1000)),
      'x-b3-traceid': b3TraceId(),
      'x-xray-traceid': xrayTraceId(Math.floor(timestampSec * 1000)),
    };
  }

  // ---------- API layer ----------
  function parseCookies(s) {
    const out = {};
    if (!s) return out;
    for (const part of s.split(';')) { const i = part.indexOf('='); if (i === -1) continue; out[part.slice(0, i).trim()] = part.slice(i + 1).trim(); }
    return out;
  }
  function baseHeaders(cookieStr) {
    return {
      accept: 'application/json, text/plain, */*', 'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
      'content-type': 'application/json;charset=UTF-8', origin: WWW, referer: WWW + '/', 'user-agent': UA,
      'sec-ch-ua': '"Not)A;Brand";v="8", "Chromium";v="138", "Microsoft Edge";v="138"', 'sec-ch-ua-mobile': '?0',
      'sec-ch-ua-platform': '"Windows"', 'sec-fetch-dest': 'empty', 'sec-fetch-mode': 'cors', 'sec-fetch-site': 'same-site',
      'x-mns': 'unload', cookie: cookieStr,
    };
  }
  function buildSignedQuery(params) {
    if (!params) return '';
    const keys = Object.keys(params);
    if (!keys.length) return '';
    return keys.map((k) => {
      const v = params[k];
      let s; if (Array.isArray(v)) s = v.map(String).join(','); else if (v !== null && v !== undefined) s = String(v); else s = '';
      return `${k}=${pyQuote(s, ',')}`;
    }).join('&');
  }
  async function signedGet(base, uri, params, cookieStr, ck, extraHeaders = {}) {
    const query = buildSignedQuery(params);
    const sig = signHeaders('GET', uri, ck, { params: params || {} });
    const resp = await fetch(base + uri + (query ? '?' + query : ''), { method: 'GET', headers: { ...baseHeaders(cookieStr), ...sig, ...extraHeaders } });
    const text = await resp.text();
    try {
      const data = JSON.parse(text);
      if (!resp.ok && data && typeof data === 'object') data.http_status = resp.status;
      return data;
    } catch (e) {
      return { success: false, msg: 'HTTP ' + resp.status + ': ' + text.slice(0, 240), raw_text: text.slice(0, 500) };
    }
  }
  async function signedPost(base, uri, payload, cookieStr, ck, extraHeaders = {}) {
    const sig = signHeaders('POST', uri, ck, { payload });
    const resp = await fetch(base + uri, { method: 'POST', headers: { ...baseHeaders(cookieStr), ...sig, ...extraHeaders }, body: JSON.stringify(payload) });
    const text = await resp.text();
    try {
      const data = JSON.parse(text);
      if (!resp.ok && data && typeof data === 'object') data.http_status = resp.status;
      return data;
    } catch (e) {
      return { success: false, msg: 'HTTP ' + resp.status + ': ' + text.slice(0, 240), raw_text: text.slice(0, 500) };
    }
  }

  function pickCover(nc) {
    const cover = nc?.cover || {};
    const url = cover.url_default || cover.url_pre || cover.url || (cover.info_list?.[0]?.url) || '';
    return url ? url.replace(/^http:\/\//, 'https://') : '';
  }
  function normItem(item) {
    const nc = item.note_card || item.noteCard || item;
    const user = nc.user || {};
    const interact = nc.interact_info || nc.interactInfo || {};
    const liked = interact.liked_count ?? interact.likedCount ?? 0;
    const id = item.id || nc.note_id || nc.id || '';
    const token = item.xsec_token || nc.xsec_token || '';
    return {
      id, note_id: id, noteId: id, xsec_token: token, xsecToken: token,
      title: nc.display_title || nc.title || '', display_title: nc.display_title || nc.title || '',
      desc: nc.desc || '', type: nc.type || item.model_type || '',
      user: { nickname: user.nickname || user.nick_name || '', user_id: user.user_id || user.userId || '' },
      nickname: user.nickname || '', author: user.nickname || '', authorId: user.user_id || '',
      interact_info: { liked_count: String(liked) }, liked_count: String(liked),
      cover: { url_default: pickCover(nc) },
    };
  }
  function normComment(c) {
    const u = c.user_info || c.user || {};
    return {
      id: c.id || '', comment_id: c.id || '', commentId: c.id || '', content: c.content || '',
      nickname: u.nickname || '', author_name: u.nickname || '',
      user: { nickname: u.nickname || '', user_id: u.user_id || '' },
      like_count: c.like_count || '0', likes: c.like_count || '0',
      sub_comments: Array.isArray(c.sub_comments) ? c.sub_comments.map(normComment) : [],
    };
  }

  async function checkLogin(cookieStr) {
    const ck = parseCookies(cookieStr);
    const r = await signedGet(EDITH, '/api/sns/web/v2/user/me', null, cookieStr, ck);
    const d = r?.data || {};
    return { logged_in: !!(r?.success && (d.user_id || d.userId || d.guest === false)), nickname: d.nickname || '', user_id: d.user_id || d.userId || '', red_id: d.red_id || '', raw: r };
  }
  async function listFeeds(cookieStr, { category = 'homefeed_recommend', cursorScore = '', noteIndex = 0, refreshType = 1 } = {}) {
    const ck = parseCookies(cookieStr);
    const payload = { cursor_score: cursorScore, num: 20, refresh_type: refreshType, note_index: noteIndex, unread_begin_note_id: '', unread_end_note_id: '', unread_note_count: 0, category, search_key: '', need_num: 10, image_formats: IMG_FORMATS, need_filter_image: false };
    const r = await signedPost(EDITH, '/api/sns/web/v1/homefeed', payload, cookieStr, ck);
    return { feeds: (r?.data?.items || []).map(normItem), cursor_score: r?.data?.cursor_score, success: !!r?.success, msg: r?.msg, raw_error: r?.success ? undefined : r };
  }
  const SORT_MAP = { general: 'general', time: 'time_descending', hot: 'popularity_descending', comment: 'comment_descending', collect: 'collect_descending' };
  function genSearchId() {
    const big = (BigInt(Date.now()) << 64n) + BigInt(Math.ceil(0x7ffffffe * Math.random()));
    const B36 = '0123456789abcdefghijklmnopqrstuvwxyz';
    let n = big, s = ''; if (n === 0n) return '0';
    while (n > 0n) { s = B36[Number(n % 36n)] + s; n /= 36n; }
    return s;
  }
  async function search(cookieStr, keyword, { page = 1, sort = 'general' } = {}) {
    const ck = parseCookies(cookieStr);
    const st = SORT_MAP[sort] || 'general';
    const payload = { keyword, page, page_size: 20, search_id: genSearchId(), sort: st, note_type: 0, ext_flags: [],
      filters: [{ tags: [st], type: 'sort_type' }, { tags: ['不限'], type: 'filter_note_type' }, { tags: ['不限'], type: 'filter_note_time' }, { tags: ['不限'], type: 'filter_note_range' }, { tags: ['不限'], type: 'filter_pos_distance' }],
      geo: '', image_formats: IMG_FORMATS };
    const r = await signedPost(EDITH, '/api/sns/web/v1/search/notes', payload, cookieStr, ck);
    const items = (r?.data?.items || []).filter((it) => it.id && (it.note_card || it.model_type === 'note'));
    return { feeds: items.map(normItem), success: !!r?.success, msg: r?.msg, raw_error: r?.success ? undefined : r };
  }
  async function getFeedDetail(cookieStr, feedId, xsecToken, { xsecSource = 'pc_feed', loadComments = true } = {}) {
    const ck = parseCookies(cookieStr);
    const payload = { source_note_id: feedId, image_formats: IMG_FORMATS, extra: { need_body_topic: '1' }, xsec_source: xsecSource || 'pc_feed', xsec_token: xsecToken || '' };
    const r = await signedPost(EDITH, '/api/sns/web/v1/feed', payload, cookieStr, ck, { 'xy-direction': '13' });
    const nc = r?.data?.items?.[0]?.note_card || {};
    const note = { note_id: feedId, title: nc.title || '', content: nc.desc || '', desc: nc.desc || '', user: nc.user || {}, interact_info: nc.interact_info || {}, image_list: nc.image_list || [], xsec_token: xsecToken || '' };
    let comments = [];
    if (loadComments) {
      try {
        const cr = await signedGet(EDITH, '/api/sns/web/v2/comment/page', { note_id: feedId, cursor: '', top_comment_id: '', image_formats: 'jpg,webp,avif', xsec_token: xsecToken || '' }, cookieStr, ck);
        comments = (cr?.data?.comments || []).map(normComment);
      } catch (e) { /* best effort */ }
    }
    return { data: { note, comments: { list: comments } }, success: !!r?.success, msg: r?.msg, raw_error: r?.success ? undefined : r };
  }
  async function userProfile(cookieStr, userId, xsecToken) {
    const ck = parseCookies(cookieStr);
    let info = await signedGet(EDITH, '/api/sns/web/v1/user/otherinfo', { target_user_id: userId }, cookieStr, ck);
    let basic = info?.data?.basic_info || {};
    if (!(info?.success && (basic.user_id || basic.userId))) {
      try {
        const byRed = await signedGet(EDITH, '/api/sns/web/v1/user/otherinfo', { red_id: userId }, cookieStr, ck);
        const redBasic = byRed?.data?.basic_info || {};
        if (byRed?.success && (redBasic.user_id || redBasic.userId)) {
          info = byRed;
          basic = redBasic;
        }
      } catch (e) { /* best effort */ }
    }
    const resolvedUserId = basic.user_id || basic.userId || userId;
    const posted = await signedGet(EDITH, '/api/sns/web/v1/user_posted', { num: 30, cursor: '', user_id: resolvedUserId, image_formats: 'jpg,webp,avif', xsec_token: xsecToken || '', xsec_source: 'pc_note' }, cookieStr, ck);
    const notes = (posted?.data?.notes || []).map(normItem);
    return { basic_info: basic || {}, notes, feeds: notes, success: !!posted?.success, msg: posted?.msg, raw_error: posted?.success ? undefined : posted, resolved_user_id: resolvedUserId };
  }
  function normUser(u) {
    const info = u?.user_info || u?.userInfo || u?.user || u?.basic_info || u || {};
    return {
      user_id: info.user_id || info.userId || info.id || u?.user_id || u?.userId || u?.id || '',
      nickname: info.nickname || info.nick_name || info.name || u?.nickname || u?.nick_name || u?.name || '',
      red_id: info.red_id || info.redId || u?.red_id || u?.redId || '',
      desc: info.desc || info.description || u?.desc || u?.description || '',
      avatar: info.image || info.avatar || info.avatar_url || u?.image || u?.avatar || u?.avatar_url || '',
      xsec_token: u?.xsec_token || u?.xsecToken || info.xsec_token || info.xsecToken || '',
    };
  }
  async function searchUsers(cookieStr, keyword, { page = 1 } = {}) {
    const ck = parseCookies(cookieStr);
    const payload = { keyword, page, page_size: 20, search_id: genSearchId() };
    const candidates = [
      '/api/sns/web/v1/search/usersearch',
      '/api/sns/web/v1/search/users',
      '/api/sns/web/v1/search/user',
    ];
    let last = null;
    for (const path of candidates) {
      try {
        const r = await signedPost(EDITH, path, payload, cookieStr, ck);
        last = r;
        const data = r?.data || {};
        const users = data.users || data.user_list || data.items || data.list || [];
        if ((r?.success && Array.isArray(users)) || (Array.isArray(users) && users.length > 0)) {
          return {
            success: !!r?.success,
            users: users.map((item) => normUser(item.user || item.user_info || item.userInfo || item)).filter(u => u.user_id || u.nickname),
            raw_error: r?.success ? undefined : r,
          };
        }
      } catch (e) {
        last = { error: e.message || String(e), path };
      }
    }
    return { success: false, users: [], error: '用户搜索接口不可用或没有匹配账号。', raw_error: last };
  }
  async function listFollowings(cookieStr, { userId = '', cursor = '', num = 50 } = {}) {
    const ck = parseCookies(cookieStr);
    if (!userId) {
      try {
        const me = await checkLogin(cookieStr);
        userId = me.user_id || '';
      } catch (e) { /* best effort */ }
    }
    if (!userId) return { success: false, error: '无法获取当前登录用户 ID，请先检查登录态。' };
    const candidates = [
      { path: '/api/sns/web/v1/user/followings', params: { user_id: userId, cursor, num } },
      { path: '/api/sns/web/v1/user/followings', params: { target_user_id: userId, cursor, num } },
      { path: '/api/sns/web/v2/user/followings', params: { user_id: userId, cursor, num } },
      { path: '/api/sns/web/v2/user/followings', params: { target_user_id: userId, cursor, num } },
      { path: '/api/sns/web/v1/user/following', params: { user_id: userId, cursor, num } },
      { path: '/api/sns/web/v1/user/following', params: { target_user_id: userId, cursor, num } },
      { path: '/api/sns/web/v2/user/following', params: { user_id: userId, cursor, num } },
      { path: '/api/sns/web/v2/user/following', params: { target_user_id: userId, cursor, num } },
      { path: '/api/sns/web/v1/user/follows', params: { user_id: userId, cursor, num } },
      { path: '/api/sns/web/v1/user/follows', params: { target_user_id: userId, cursor, num } },
    ];
    let last = null;
    for (const c of candidates) {
      try {
        const r = await signedGet(EDITH, c.path, c.params, cookieStr, ck);
        last = r;
        const data = r?.data || {};
        const users = data.users || data.user_list || data.list || data.items || data.followings || data.following_list || [];
        if ((r?.success && Array.isArray(users)) || (Array.isArray(users) && users.length > 0)) {
          return {
            success: !!r?.success,
            users: Array.isArray(users) ? users.map(normUser).filter(u => u.user_id || u.nickname) : [],
            cursor: data.cursor || data.next_cursor || data.nextCursor || '',
            has_more: !!(data.has_more || data.hasMore),
            raw_error: r?.success ? undefined : r,
          };
        }
      } catch (e) {
        last = { error: e.message || String(e), path: c.path };
      }
    }
    return { success: false, users: [], error: '关注列表接口不可用或已被小红书改版。', raw_error: last };
  }
  async function likeFeed(cookieStr, feedId, unlike = false) {
    const ck = parseCookies(cookieStr);
    const r = await signedPost(EDITH, unlike ? '/api/sns/web/v1/note/dislike' : '/api/sns/web/v1/note/like', { note_oid: feedId }, cookieStr, ck);
    return { success: !!r?.success, msg: r?.msg, raw: r };
  }
  async function favoriteFeed(cookieStr, feedId, unfavorite = false) {
    const ck = parseCookies(cookieStr);
    const r = await signedPost(EDITH, unfavorite ? '/api/sns/web/v1/note/uncollect' : '/api/sns/web/v1/note/collect', unfavorite ? { note_ids: feedId } : { note_id: feedId }, cookieStr, ck);
    return { success: !!r?.success, msg: r?.msg, raw: r };
  }
  async function postComment(cookieStr, feedId, content, { targetCommentId = null, xsecToken = '' } = {}) {
    const ck = parseCookies(cookieStr);
    const payload = { note_id: feedId, content, at_users: [] };
    if (xsecToken) payload.xsec_token = xsecToken;
    if (targetCommentId) payload.target_comment_id = targetCommentId;
    const r = await signedPost(EDITH, '/api/sns/web/v1/comment/post', payload, cookieStr, ck);
    return { success: !!r?.success, msg: r?.msg, comment: r?.data?.comment, raw: r };
  }

  async function sha1Hex(str) {
    const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(str));
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  async function hmacSha1Hex(key, msg) {
    const k = await crypto.subtle.importKey('raw', new TextEncoder().encode(key), { name: 'HMAC', hash: 'SHA-1' }, false, ['sign']);
    const sig = await crypto.subtle.sign('HMAC', k, new TextEncoder().encode(msg));
    return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  async function cosUploadSignature(message, fileId, contentLength, host) {
    host = host || 'ros-upload.xiaohongshu.com';
    const signKey = await hmacSha1Hex('null', message);
    const params = await sha1Hex(`put\n/spectrum/${fileId}\n\ncontent-length=${contentLength}&host=${host}\n`);
    return hmacSha1Hex(signKey, `sha1\n${message}\n${params}\n`);
  }
  function imageSize(buf) {
    try {
      if (buf[0] === 0x89 && buf[1] === 0x50) { const dv = new DataView(buf.buffer); return { width: dv.getUint32(16), height: dv.getUint32(20) }; }
      if (buf[0] === 0xff && buf[1] === 0xd8) {
        let o = 2;
        while (o < buf.length) {
          if (buf[o] !== 0xff) { o++; continue; }
          const marker = buf[o + 1];
          if (marker >= 0xc0 && marker <= 0xc3) { const dv = new DataView(buf.buffer); return { height: dv.getUint16(o + 5), width: dv.getUint16(o + 7) }; }
          o += 2 + ((buf[o + 2] << 8) | buf[o + 3]);
        }
      }
      if (buf[8] === 0x57 && buf[9] === 0x45 && buf[12] === 0x56 && buf[15] === 0x20) {
        return { width: ((buf[27] << 8) | buf[26]) & 0x3fff, height: ((buf[29] << 8) | buf[28]) & 0x3fff };
      }
    } catch (e) { /* ignore */ }
    return null;
  }
  // 上传凭证：不同登录态/版本接口名不同，依次尝试，取第一个成功的
  async function getUploadPermit(cookieStr, ck) {
    const params = { biz_name: 'spectrum', scene: 'image', file_count: '1', version: '1', source: 'web' };
    const candidates = [
      { host: EDITH, path: '/api/media/v1/upload/web/permit', origin: WWW, referer: WWW + '/' },
      { host: CREATOR, path: '/api/media/v1/upload/creator/permit', origin: CREATOR, referer: CREATOR + '/publish/publish' },
      { host: EDITH, path: '/api/media/v1/upload/creator/permit', origin: CREATOR, referer: CREATOR + '/publish/publish' },
      { host: CREATOR, path: '/api/media/v1/upload/web/permit', origin: WWW, referer: WWW + '/' },
    ];
    let lastErr = '';
    for (const c of candidates) {
      try {
        const sig = signHeaders('GET', c.path, ck, { params });
        const resp = await fetch(c.host + c.path + '?' + buildSignedQuery(params), { method: 'GET', headers: { ...baseHeaders(cookieStr), ...sig, origin: c.origin, referer: c.referer } });
        const j = await resp.json().catch(() => ({}));
        const permit = j?.data?.uploadTempPermits?.[0];
        if (permit) return { permit, xt: sig['x-t'] };
        lastErr = `${c.path}@${c.host.replace('https://', '')} -> ${JSON.stringify(j).slice(0, 120)}`;
      } catch (e) { lastErr = `${c.path}: ${e.message}`; }
    }
    throw new Error('获取上传凭证失败（已试多种接口）: ' + lastErr);
  }
  async function uploadImageFromUrl(cookieStr, ck, imgUrl) {
    const imgResp = await fetch(imgUrl);
    if (!imgResp.ok) throw new Error(`图片下载失败 ${imgResp.status}: ${imgUrl}`);
    const buf = new Uint8Array(await imgResp.arrayBuffer());
    const mime = imgResp.headers.get('content-type') || 'image/png';
    const { width, height } = imageSize(buf) || { width: 1080, height: 1080 };
    const { permit, xt } = await getUploadPermit(cookieStr, ck);
    const fileIds = permit.fileIds[0].split('/').pop();
    const uploadAddr = permit.uploadAddr || 'ros-upload.xiaohongshu.com';
    const uploadHost = uploadAddr.replace(/^https?:\/\//, '');
    const uploadBase = uploadAddr.startsWith('http') ? uploadAddr : `https://${uploadAddr}`;
    const message = `${String(xt).slice(0, 10)};${String(permit.expireTime).slice(0, 10)}`;
    const signature = await cosUploadSignature(message, fileIds, buf.length, uploadHost);
    const putResp = await fetch(`${uploadBase}/spectrum/${fileIds}`, {
      method: 'PUT',
      headers: { accept: '*/*', authorization: `q-sign-algorithm=sha1&q-ak=null&q-sign-time=${message}&q-key-time=${message}&q-header-list=content-length;host&q-url-param-list=&q-signature=${signature}`, origin: CREATOR, referer: CREATOR + '/', 'user-agent': UA, 'x-cos-security-token': permit.token, cookie: cookieStr },
      body: buf,
    });
    if (!putResp.ok) throw new Error(`图片上传失败 ${putResp.status}`);
    return { fileIds, width, height, file_size: buf.length, mime_type: mime };
  }
  function buildImageNoteData(title, desc, privacyType, fileInfos, hashTags) {
    const images = fileInfos.map((f) => ({
      file_id: `spectrum/${f.fileIds}`, width: f.width, height: f.height, metadata: { source: -1 }, stickers: { version: 2, floating: [] },
      extra_info_json: JSON.stringify({ mimeType: f.mime_type || 'image/png', image_metadata: { bg_color: '', origin_size: (f.file_size || 0) / 1024 } }),
    }));
    const contextJson = JSON.stringify({ recommend_title: { recommend_title_id: '', is_use: 3, used_index: -1 }, recommendTitle: [], recommend_topics: { used: [] } });
    return {
      common: { type: 'normal', title, note_id: '', desc, source: '{"type":"web","ids":"","extraInfo":"{\\"subType\\":\\"official\\",\\"systemId\\":\\"web\\"}"}', ats: [], hash_tag: hashTags, post_loc: {}, privacy_info: { op_type: 1, type: privacyType, user_ids: [] }, goods_info: {}, biz_relations: [], capa_trace_info: { contextJson } },
      image_info: { images }, video_info: null,
    };
  }
  async function publishNote(cookieStr, { title = '', content = '', images = [], tags = [], isPrivate = false }) {
    const ck = parseCookies(cookieStr);
    const fileInfos = [];
    for (const imgUrl of images) fileInfos.push(await uploadImageFromUrl(cookieStr, ck, imgUrl));
    if (!fileInfos.length) return { error: '小红书发帖至少需要一张图片，请提供 images（图床 URL 数组）' };
    let desc = content;
    const hashTags = [];
    for (const t of tags) { const name = String(t).replace(/^#/, ''); desc += ` #${name}[话题]#`; hashTags.push({ id: '', link: '', name, type: 'topic' }); }
    const r = await signedPost(EDITH, '/web_api/sns/v2/note', buildImageNoteData(title, desc, isPrivate ? 1 : 0, fileInfos, hashTags), cookieStr, ck);
    const noteId = r?.data?.id || r?.data?.note_id || r?.data?.note?.id || '';
    // 失败用 error 字段：bridgePost 会据此判定 success=false（无需改 useChatAI）
    if (!(r?.success && noteId)) {
      return { error: `发布失败（小红书未确认）: ${JSON.stringify(r).slice(0, 300)}` };
    }
    return { success: true, note_id: noteId, noteId, msg: '发布成功', raw: r };
  }

  async function handle(command, body, cookie) {
    switch (command) {
      case 'check-login': return checkLogin(cookie);
      case 'search': return search(cookie, body.keyword || '', { sort: body.sort_by, page: body.page });
      case 'search-users': return searchUsers(cookie, body.keyword || '', { page: body.page });
      case 'list-feeds': return listFeeds(cookie, { category: body.category, cursorScore: body.cursor_score, noteIndex: body.note_index });
      case 'get-feed-detail': return getFeedDetail(cookie, body.feed_id, body.xsec_token, { xsecSource: body.xsec_source, loadComments: body.load_all_comments !== false });
      case 'post-comment': return postComment(cookie, body.feed_id, body.content, { xsecToken: body.xsec_token });
      case 'reply-comment': return postComment(cookie, body.feed_id, body.content, { targetCommentId: body.comment_id, xsecToken: body.xsec_token });
      case 'like-feed': return likeFeed(cookie, body.feed_id, !!body.unlike);
      case 'favorite-feed': return favoriteFeed(cookie, body.feed_id, !!body.unfavorite);
      case 'user-profile': return userProfile(cookie, body.user_id, body.xsec_token);
      case 'list-followings': return listFollowings(cookie, { userId: body.user_id, cursor: body.cursor, num: body.num });
      case 'publish': return publishNote(cookie, { title: body.title, content: body.content, images: body.images || [], tags: body.tags || [], isPrivate: body.visibility === 'private' || !!body.is_private });
      case 'login': return { error: 'lite 模式用 cookie 登录，无需扫码。请在设置里粘贴 cookie。' };
      case 'get-qrcode': return { error: 'lite 模式不支持二维码登录，请粘贴 cookie。' };
      case 'delete-cookies': return { ok: true };
      case 'publish-video': return { error: '视频发布暂未在 lite 模式实现。' };
      case 'long-article': return { error: '长文发布暂未在 lite 模式实现。' };
      default: return null;
    }
  }

  return { handle, __test: { RNG, signXs, signXsCommon, generateB1, _internals: { md5Hex, encodeCustomStr, crc32JsInt } } };
})();

// 供 Node 验证用（Worker 运行时忽略多余的具名导出）。见 worker/xhs-lite/test/verify.mjs
export const __xhsLiteTest = XHSLite.__test;

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin") || "*";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    const apiMatch = url.pathname.match(/^\/api\/(.+)$/);
    if (apiMatch) {
      const command = apiMatch[1].replace(/\/+$/, "");
      if (command === "health") {
        return jsonResponse({ status: "ok", backend: "xhs-lite", signing: "xhshow-pure-js" }, { origin });
      }
      let body = {};
      if (request.method === "POST") { try { body = await request.json(); } catch (e) { /* allow empty */ } }
      const cookie = request.headers.get("x-xhs-cookie") || body.cookie || (env && env.XHS_COOKIE) || "";
      if (!cookie) return jsonResponse({ error: "未配置 cookie。请在设置里粘贴小红书 cookie。" }, { status: 401, origin });
      if (!cookie.includes("a1=")) return jsonResponse({ error: "cookie 缺少 a1 字段，请复制完整的小红书 cookie。" }, { status: 400, origin });
      try {
        const result = await XHSLite.handle(command, body, cookie);
        if (result === null) return jsonResponse({ error: "Unknown command: " + command }, { status: 404, origin });
        return jsonResponse(result, { origin });
      } catch (e) {
        return jsonResponse({ error: e.message || String(e) }, { status: 500, origin });
      }
    }

    return jsonResponse({ error: "Not found. Use /api/health, /api/check-login, /api/search, /api/list-feeds, /api/get-feed-detail, /api/post-comment, /api/reply-comment, /api/like-feed, /api/favorite-feed, /api/user-profile, /api/list-followings, /api/publish" }, { status: 404, origin });
  }
};

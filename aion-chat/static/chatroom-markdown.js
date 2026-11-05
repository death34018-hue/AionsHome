(function (root, factory) {
  const markdownItFactory = root.markdownit
    || (typeof module === 'object' && module.exports
      ? require('./vendor/markdown-it-15.0.1.min.js')
      : null);
  const api = factory(markdownItFactory);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.ChatroomMarkdown = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (markdownItFactory) {
  'use strict';

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function stripMarkdownImages(text) {
    return text.replace(/!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)/g, '$1');
  }

  function stripUnsafeLinks(text) {
    return text.replace(
      /\[([^\]]+)\]\(\s*(?:javascript|vbscript|file|data):(?:[^()]|\([^)]*\))*\)/gi,
      '$1',
    );
  }

  const parser = markdownItFactory
    ? markdownItFactory({
        html: false,
        breaks: true,
        linkify: true,
        typographer: false,
      }).disable(['image', 'strikethrough'])
    : null;

  if (parser) {
    // Empty layout spans indent explicit breaks without changing copied text.
    for (const type of ['softbreak', 'hardbreak']) {
      const defaultBreak = parser.renderer.rules[type];
      parser.renderer.rules[type] = (tokens, index, options, env, renderer) =>
        defaultBreak(tokens, index, options, env, renderer)
        + (env.indentLineBreaks ? '<span class="line-indent" aria-hidden="true"></span>' : '');
    }
    // CommonMark rejects 。**汉字 and 汉字**“ as emphasis boundaries.
    // Relax only these Han/punctuation boundaries for double stars, leaving
    // pairing, nested formatting, code and escapes to markdown-it.
    parser.inline.ruler.before('emphasis', 'chinese_strong', (state, silent) => {
      if (silent || state.src.charCodeAt(state.pos) !== 0x2A) return false;
      const scanned = state.scanDelims(state.pos, true);
      if (scanned.length !== 2) return false;
      const before = state.src.slice(0, state.pos).match(/.$/u)?.[0] || '';
      const after = String.fromCodePoint(state.src.codePointAt(state.pos + 2) || 0);
      const opens = /\p{Script=Han}/u.test(before) && /\p{P}/u.test(after);
      const closes = /\p{P}/u.test(before) && /\p{Script=Han}/u.test(after);
      if (!opens && !closes) return false;

      for (let index = 0; index < 2; index += 1) {
        const token = state.push('text', '', 0);
        token.content = '*';
        state.delimiters.push({
          marker: 0x2A,
          length: 2,
          token: state.tokens.length - 1,
          end: -1,
          open: scanned.can_open || opens,
          close: scanned.can_close || closes,
        });
      }
      state.pos += 2;
      return true;
    });

    const defaultLinkOpen = parser.renderer.rules.link_open
      || ((tokens, index, options, env, renderer) => renderer.renderToken(tokens, index, options));
    parser.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
      tokens[index].attrSet('rel', 'noopener noreferrer');
      return defaultLinkOpen(tokens, index, options, env, renderer);
    };
  }

  function render(value, options = {}) {
    const safeMarkdown = stripUnsafeLinks(stripMarkdownImages(String(value ?? '')));
    if (!parser) return `<p>${escapeHtml(safeMarkdown).replace(/\n/g, '<br>')}</p>`;
    return parser.render(safeMarkdown, options);
  }

  function splitUserBubbleParts(value) {
    const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
    const parts = [];
    let buffer = [];
    let mode = '';
    let fenceChar = '';
    let fenceLength = 0;

    const flush = () => {
      const part = buffer.join('\n').trim();
      if (part) parts.push(part);
      buffer = [];
      mode = '';
    };
    const lineMode = trimmed => {
      if (/^(?:[-*+]\s+|\d+[.)]\s+)/.test(trimmed)) return 'list';
      if (/^>\s?/.test(trimmed)) return 'quote';
      if (/^\|/.test(trimmed)) return 'table';
      return 'plain';
    };

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index].trimEnd();
      const trimmed = line.trim();

      if (mode === 'fence') {
        buffer.push(line);
        const closing = trimmed.match(/^(`+|~+)\s*$/);
        if (closing && closing[1][0] === fenceChar && closing[1].length >= fenceLength) flush();
        continue;
      }

      const opening = trimmed.match(/^(`{3,}|~{3,})/);
      if (opening) {
        flush();
        mode = 'fence';
        fenceChar = opening[1][0];
        fenceLength = opening[1].length;
        buffer.push(line);
        continue;
      }

      if (!trimmed) {
        const nextTrimmed = String(lines[index + 1] ?? '').trim();
        if (mode && mode !== 'plain' && lineMode(nextTrimmed) === mode) {
          buffer.push('');
        } else {
          flush();
        }
        continue;
      }

      if (/^#{1,6}\s+/.test(trimmed)) {
        flush();
        parts.push(trimmed);
        continue;
      }

      const nextMode = lineMode(trimmed);
      if (nextMode !== 'plain' || (mode === 'list' && /^\s{2,}\S/.test(line))) {
        const structuredMode = nextMode === 'plain' ? mode : nextMode;
        if (mode && mode !== structuredMode) flush();
        mode = structuredMode;
        buffer.push(line);
        continue;
      }

      flush();
      parts.push(trimmed);
    }

    flush();
    return parts;
  }

  return { escapeHtml, render, splitUserBubbleParts };
});

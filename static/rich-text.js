(function () {
  'use strict';

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function safeHttpUrl(value) {
    try {
      const url = new URL(String(value || ''));
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch {
      return '';
    }
  }

  function renderInline(value) {
    let source = String(value ?? '');
    const tokens = [];
    const token = html => {
      const key = '\uE000' + tokens.length + '\uE001';
      tokens.push(html);
      return key;
    };

    source = source.replace(/\x60([^\x60\n]+)\x60/g, (_, code) => token('<code>' + escapeHtml(code) + '</code>'));
    source = source.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/gi, (match, label, href) => {
      const safe = safeHttpUrl(href);
      return safe ? token('<a href="' + escapeHtml(safe) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(label) + '</a>') : match;
    });
    source = source.replace(/https?:\/\/[^\s<>()]+/gi, match => {
      const trimmed = match.replace(/[.,!?;:]+$/g, '');
      const suffix = match.slice(trimmed.length);
      const safe = safeHttpUrl(trimmed);
      return safe ? token('<a href="' + escapeHtml(safe) + '" target="_blank" rel="noopener noreferrer">Открыть ссылку</a>') + suffix : match;
    });

    let html = escapeHtml(source)
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_\n]+)__/g, '<strong>$1</strong>')
      .replace(/~~([^~\n]+)~~/g, '<del>$1</del>')
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,!?;:])/g, '$1<em>$2</em>')
      .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?;:])/g, '$1<em>$2</em>');
    html = html.replace(/\uE000(\d+)\uE001/g, (_, index) => tokens[Number(index)] || '');
    return html;
  }

  function splitTableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
  }

  function isTableDivider(line) {
    const cells = splitTableRow(line);
    return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  }

  function normalize(value) {
    return String(value ?? '')
      .replace(/\r\n?/g, '\n')
      .replace(/[ \t]+$/gm, '')
      .replace(/\s*(Обращение H-[A-ZА-Я0-9-]+ уже передано человеку; ожидайте ответа специалиста\.)/giu, '\n\n$1')
      .trim();
  }

  function renderMarkdown(value) {
    const source = normalize(value);
    if (!source) return '<div class="rich-text"><p></p></div>';
    const lines = source.split('\n');
    const output = [];
    let paragraph = [];
    let listType = '';
    let listItems = [];
    let quote = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      output.push('<p>' + paragraph.map(renderInline).join('<br>') + '</p>');
      paragraph = [];
    };
    const flushList = () => {
      if (!listItems.length) return;
      output.push('<' + listType + '>' + listItems.map(item => '<li>' + renderInline(item) + '</li>').join('') + '</' + listType + '>');
      listItems = [];
      listType = '';
    };
    const flushQuote = () => {
      if (!quote.length) return;
      output.push('<blockquote>' + quote.map(renderInline).join('<br>') + '</blockquote>');
      quote = [];
    };
    const flushAll = () => {
      flushParagraph();
      flushList();
      flushQuote();
    };

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const trimmed = line.trim();
      if (!trimmed) {
        flushAll();
        continue;
      }

      if (/^\x60\x60\x60\s*[\w+-]*\s*$/.test(trimmed)) {
        flushAll();
        const code = [];
        index += 1;
        while (index < lines.length && !/^\x60\x60\x60\s*$/.test(lines[index].trim())) {
          code.push(lines[index]);
          index += 1;
        }
        output.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
        continue;
      }

      if (line.includes('|') && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        flushAll();
        const headers = splitTableRow(line);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        index -= 1;
        output.push('<div class="rich-table-scroll"><table><thead><tr>' + headers.map(cell => '<th>' + renderInline(cell) + '</th>').join('') + '</tr></thead><tbody>' + rows.map(row => '<tr>' + headers.map((_, cellIndex) => '<td>' + renderInline(row[cellIndex] || '') + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>');
        continue;
      }

      const heading = trimmed.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        flushAll();
        const level = Math.min(4, Math.max(3, heading[1].length + 2));
        output.push('<h' + level + '>' + renderInline(heading[2]) + '</h' + level + '>');
        continue;
      }
      if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        flushAll();
        output.push('<hr>');
        continue;
      }
      const blockquote = trimmed.match(/^>\s?(.*)$/);
      if (blockquote) {
        flushParagraph();
        flushList();
        quote.push(blockquote[1]);
        continue;
      }
      const unordered = trimmed.match(/^(?:[-+*•])\s+(.+)$/);
      const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushParagraph();
        flushQuote();
        const nextType = ordered ? 'ol' : 'ul';
        if (listType && listType !== nextType) flushList();
        listType = nextType;
        listItems.push((ordered || unordered)[1]);
        continue;
      }

      flushList();
      flushQuote();
      paragraph.push(trimmed);
    }
    flushAll();
    return '<div class="rich-text">' + output.join('') + '</div>';
  }

  window.ConsiliumRichText = Object.freeze({ render: renderMarkdown, escapeHtml });
}());

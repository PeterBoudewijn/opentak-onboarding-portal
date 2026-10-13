import React, { useMemo } from 'react';

const ALLOWED_TAGS = new Set([
  'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's',
  'blockquote', 'pre', 'code',
  'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'a', 'span', 'div',
]);

const DROP_CONTENT_TAGS = new Set([
  'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math',
  'form', 'input', 'button', 'textarea', 'select', 'option',
  'link', 'meta', 'base',
]);

function safeHref(value) {
  const href = String(value || '').trim();
  if (!href) return null;

  if (href.startsWith('/') || href.startsWith('#')) {
    return href;
  }

  try {
    const parsed = new URL(href, window.location.origin);
    if (['http:', 'https:', 'mailto:', 'tel:'].includes(parsed.protocol)) {
      return href;
    }
  } catch {
    return null;
  }

  return null;
}

function sanitizeClassName(value) {
  const safe = String(value || '')
    .split(/\s+/)
    .filter((name) => /^ql-[a-z0-9_-]+$/i.test(name));
  return safe.length ? safe.join(' ') : undefined;
}

function renderNode(node, key) {
  if (node.nodeType === Node.TEXT_NODE) {
    return node.textContent;
  }

  if (node.nodeType !== Node.ELEMENT_NODE) {
    return null;
  }

  const tag = node.tagName.toLowerCase();
  if (DROP_CONTENT_TAGS.has(tag)) {
    return null;
  }

  const children = Array.from(node.childNodes)
    .map((child, index) => renderNode(child, `${key}-${index}`))
    .filter((child) => child !== null && child !== undefined);

  if (!ALLOWED_TAGS.has(tag)) {
    return <React.Fragment key={key}>{children}</React.Fragment>;
  }

  const props = { key };
  const className = sanitizeClassName(node.getAttribute('class'));
  if (className) props.className = className;

  if (tag === 'a') {
    const href = safeHref(node.getAttribute('href'));
    if (href) {
      props.href = href;
      props.rel = 'noopener noreferrer';
      const target = node.getAttribute('target');
      if (target === '_blank') props.target = '_blank';
    }
    const title = node.getAttribute('title');
    if (title) props.title = title;
  }

  return React.createElement(tag, props, children);
}

export default function SafeHtml({ html, className }) {
  const content = useMemo(() => {
    const parser = new DOMParser();
    const document = parser.parseFromString(String(html || ''), 'text/html');
    return Array.from(document.body.childNodes)
      .map((node, index) => renderNode(node, `safe-html-${index}`))
      .filter((node) => node !== null && node !== undefined);
  }, [html]);

  return <div className={className}>{content}</div>;
}

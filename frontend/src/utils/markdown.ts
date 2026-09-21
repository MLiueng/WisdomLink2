/**
 * Markdown 渲染统一出口：marked 解析后经 DOMPurify 消毒再交给 v-html，防 XSS。
 * DOMPurify 默认保留 data-* 属性，正文引用角标（data-seq）等自定义标记不受影响。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export function renderMarkdown(md: string): string {
  const html = marked.parse(md || '', { async: false }) as string
  return DOMPurify.sanitize(html)
}

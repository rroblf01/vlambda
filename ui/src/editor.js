/*!
 * CodeMirror 6 (codemirror, @codemirror/*)
 * Copyright (C) 2018-2021 by Marijn Haverbeke <marijn@haverbeke.berlin> and others
 * Released under the MIT License.
 */
import { EditorView, basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { Chart } from 'chart.js/auto';

window.Chart = Chart;

// Colours come from CSS variables so the editor follows the UI light/dark theme.
const vlambdaTheme = EditorView.theme({
  '&': {
    backgroundColor: 'var(--ed-bg)',
    color: 'var(--ed-fg)',
    height: '100%'
  },
  '.cm-scroller': {
    fontFamily: '"JetBrains Mono", "Fira Code", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
    fontSize: '13.5px',
    lineHeight: '1.65'
  },
  '.cm-content': {
    caretColor: 'var(--ed-cursor)',
    padding: '12px 0'
  },
  '.cm-line': { padding: '0 14px' },
  '.cm-gutters': {
    backgroundColor: 'var(--ed-gutter-bg)',
    color: 'var(--ed-gutter)',
    borderRight: '1px solid var(--ed-border)'
  },
  '.cm-gutterElement': { padding: '0 10px 0 14px' },
  '.cm-activeLine': { backgroundColor: 'var(--ed-active)' },
  '.cm-activeLineGutter': {
    backgroundColor: 'var(--ed-active-gutter)',
    color: 'var(--ed-fg)'
  },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection': {
    backgroundColor: 'var(--ed-selection) !important'
  },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--ed-cursor)' },
  '.cm-panels': {
    backgroundColor: 'var(--ed-panel)',
    color: 'var(--ed-fg)'
  },
  '.cm-panels.cm-panels-top': { borderBottom: '1px solid var(--ed-border)' },
  '.cm-panels.cm-panels-bottom': { borderTop: '1px solid var(--ed-border)' },
  '.cm-searchMatch': {
    backgroundColor: 'rgba(210, 153, 34, .25)',
    outline: '1px solid rgba(210, 153, 34, .6)'
  },
  '.cm-searchMatch.cm-searchMatch-selected': {
    backgroundColor: 'rgba(210, 153, 34, .45)'
  },
  '.cm-tooltip': {
    backgroundColor: 'var(--ed-panel)',
    border: '1px solid var(--ed-border)'
  },
  '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
    backgroundColor: 'var(--ed-selection)',
    color: 'var(--ed-fg)'
  },
  '.cm-foldPlaceholder': {
    backgroundColor: 'var(--ed-panel)',
    border: '1px solid var(--ed-border)',
    color: 'var(--ed-gutter)'
  }
});

const vlambdaHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: 'var(--syn-keyword)' },
  { tag: [tags.name, tags.deleted, tags.character, tags.propertyName, tags.macroName], color: 'var(--syn-name)' },
  { tag: [tags.function(tags.variableName), tags.labelName], color: 'var(--syn-function)' },
  { tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)], color: 'var(--syn-constant)' },
  { tag: [tags.definition(tags.name), tags.separator], color: 'var(--syn-name)' },
  { tag: [tags.typeName, tags.className, tags.number, tags.changed, tags.annotation, tags.modifier, tags.self, tags.namespace], color: 'var(--syn-type)' },
  { tag: [tags.operator, tags.operatorKeyword, tags.url, tags.escape, tags.regexp, tags.link, tags.special(tags.string)], color: 'var(--syn-operator)' },
  { tag: [tags.meta, tags.comment], color: 'var(--syn-comment)' },
  { tag: tags.strong, fontWeight: 'bold' },
  { tag: tags.emphasis, fontStyle: 'italic' },
  { tag: tags.strikethrough, textDecoration: 'line-through' },
  { tag: tags.link, color: 'var(--syn-comment)', textDecoration: 'underline' },
  { tag: tags.heading, fontWeight: 'bold', color: 'var(--syn-constant)' },
  { tag: [tags.atom, tags.bool, tags.special(tags.variableName)], color: 'var(--syn-constant)' },
  { tag: [tags.processingInstruction, tags.string, tags.inserted], color: 'var(--syn-string)' },
  { tag: tags.invalid, color: 'var(--syn-invalid)' }
]);

const cursorStatus = EditorView.updateListener.of(update => {
  if (!update.selectionSet && !update.docChanged) return;
  const head = update.state.selection.main.head;
  const line = update.state.doc.lineAt(head);
  update.view.dom.dispatchEvent(new CustomEvent('cm-cursor', {
    bubbles: true,
    detail: { line: line.number, col: head - line.from + 1 }
  }));
});

export function createEditor(parent, initialText) {
  const view = new EditorView({
    doc: initialText || '',
    extensions: [
      basicSetup,
      python(),
      vlambdaTheme,
      syntaxHighlighting(vlambdaHighlight),
      cursorStatus
    ],
    parent
  });
  return view;
}

export function getValue(view) {
  return view.state.doc.toString();
}

export function setValue(view, text) {
  view.dispatch({
    changes: { from: 0, to: view.state.doc.length, insert: text }
  });
}

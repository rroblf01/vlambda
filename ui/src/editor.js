import { EditorView, basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';
import { Chart } from 'chart.js/auto';

window.Chart = Chart;

export function createEditor(parent, initialText) {
  const view = new EditorView({
    doc: initialText || '',
    extensions: [basicSetup, python()],
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

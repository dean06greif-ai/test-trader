import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Plus, PencilSimple, Check, X, CaretLeft, CaretRight, Trash } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import useDragScroll from '../hooks/useDragScroll';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;
const STORE_KEY = 'krypto_ai_quick_prompts';

export const DEFAULT_QUICK_PROMPTS = [
  'Wie ist deine aktuelle Performance?',
  'Was hast du zuletzt gelernt?',
  'Sei heute defensiv',
  'Begründe deine letzte Entscheidung',
];

// Lokaler Cache: zeigt die Chips sofort an, bevor der Server antwortet
const cached = () => {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
    if (Array.isArray(parsed) && parsed.length) {
      return parsed.filter(p => typeof p === 'string' && p.trim()).slice(0, 30);
    }
  } catch (e) { /* ignore */ }
  return DEFAULT_QUICK_PROMPTS;
};

/**
 * Schnellauswahl der Chat-Vorschläge: EINE Zeile, seitwärts wischbar
 * (Touch nativ, Maus per Ziehen), eigene Vorschläge anlegen, verschieben,
 * löschen. Gespeichert wird serverseitig (geräteübergreifend); localStorage
 * dient nur als Sofort-Anzeige und für die einmalige Übernahme alter Listen.
 */
const AIQuickPrompts = ({ onPick, disabled = false }) => {
  const [prompts, setPrompts] = useState(cached);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null);
  const drag = useDragScroll({ wheel: false });
  const migrated = useRef(false);

  const save = useCallback(async (next) => {
    setPrompts(next);
    try { localStorage.setItem(STORE_KEY, JSON.stringify(next)); } catch (e) { /* ignore */ }
    try {
      const res = await fetch(`${API_URL}/api/ai/quick-prompts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ prompts: next }),
      });
      if (!res.ok) throw new Error();
    } catch (e) {
      toast.error('Vorschläge konnten nicht gespeichert werden (nur lokal übernommen)');
    }
  }, []);

  // Serverstand laden; eine lokal gepflegte Liste wird einmalig übernommen
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const data = await fetch(`${API_URL}/api/ai/quick-prompts`).then(r => r.json());
        if (!alive || !Array.isArray(data.prompts)) return;
        const local = cached();
        const localIsCustom = JSON.stringify(local) !== JSON.stringify(DEFAULT_QUICK_PROMPTS);
        if (!data.customized && localIsCustom && !migrated.current) {
          migrated.current = true;
          save(local);
          return;
        }
        setPrompts(data.prompts);
        try { localStorage.setItem(STORE_KEY, JSON.stringify(data.prompts)); } catch (e) { /* ignore */ }
      } catch (e) { /* offline: lokaler Cache bleibt sichtbar */ }
    })();
    return () => { alive = false; };
  }, [save]);

  const move = (index, dir) => {
    const target = index + dir;
    if (target < 0 || target >= prompts.length) return;
    const next = [...prompts];
    [next[index], next[target]] = [next[target], next[index]];
    save(next);
  };

  const remove = (index) => save(prompts.filter((_, i) => i !== index));

  const saveDraft = () => {
    const text = (draft || '').trim();
    if (!text) { setDraft(null); return; }
    save([...prompts, text].slice(0, 30));
    setDraft('');
  };

  return (
    <div className="ai-quick-prompts" data-testid="ai-quick-prompts">
      <div
        className={`ai-quick-strip ${editing ? 'editing' : ''}`}
        {...drag.props}
        data-testid="ai-quick-prompts-strip"
      >
        {prompts.map((q, i) => (
          <div className="ai-quick-item" key={`${q}-${i}`} data-testid={`ai-quick-item-${i}`}>
            {editing && (
              <button className="ai-quick-mini" onClick={() => move(i, -1)}
                title="nach links" data-testid={`ai-quick-left-${i}`}>
                <CaretLeft size={11} weight="bold" />
              </button>
            )}
            <button
              className="ai-quick-chip"
              onClick={() => onPick(q)}
              disabled={disabled}
              title={q}
              data-testid={`ai-quick-chip-${i}`}
            >
              {q}
            </button>
            {editing && (
              <>
                <button className="ai-quick-mini" onClick={() => move(i, 1)}
                  title="nach rechts" data-testid={`ai-quick-right-${i}`}>
                  <CaretRight size={11} weight="bold" />
                </button>
                <button className="ai-quick-mini danger" onClick={() => remove(i)}
                  title="löschen" data-testid={`ai-quick-delete-${i}`}>
                  <Trash size={11} weight="bold" />
                </button>
              </>
            )}
          </div>
        ))}
        {draft !== null && (
          <div className="ai-quick-item">
            <input
              className="ai-quick-input"
              autoFocus
              value={draft}
              placeholder="Eigener Vorschlag…"
              onChange={e => setDraft(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter') saveDraft();
                if (e.key === 'Escape') setDraft(null);
              }}
              data-testid="ai-quick-new-input"
            />
            <button className="ai-quick-mini" onClick={saveDraft} title="speichern"
              data-testid="ai-quick-new-save">
              <Check size={11} weight="bold" />
            </button>
            <button className="ai-quick-mini" onClick={() => setDraft(null)} title="abbrechen"
              data-testid="ai-quick-new-cancel">
              <X size={11} weight="bold" />
            </button>
          </div>
        )}
      </div>
      <div className="ai-quick-tools">
        <button className="ai-quick-tool" onClick={() => setDraft(draft === null ? '' : null)}
          title="Eigenen Vorschlag hinzufügen" data-testid="ai-quick-add-btn">
          <Plus size={13} weight="bold" />
        </button>
        <button className={`ai-quick-tool ${editing ? 'active' : ''}`}
          onClick={() => setEditing(v => !v)}
          title={editing ? 'Bearbeiten beenden' : 'Vorschläge bearbeiten (verschieben/löschen)'}
          data-testid="ai-quick-edit-btn">
          {editing ? <Check size={13} weight="bold" /> : <PencilSimple size={13} weight="bold" />}
        </button>
      </div>
    </div>
  );
};

export default AIQuickPrompts;

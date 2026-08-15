import React, { useState, useEffect, useCallback } from 'react';
import { Clock, Plus, Trash, FloppyDisk, BellSlash } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import { MODEL_OPTIONS } from '../lib/aiModels';
import './AIGovernance.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const PRESETS = [
  { label: 'Nacht ruhiger', from: '22:00', to: '06:00', interval_min: 30 },
  { label: 'US-Open aktiv', from: '15:00', to: '18:00', interval_min: 5 },
];

/** Analyse-Zeitplan der KI (Intervalle je Zeitfenster) + Telegram-Sperrzeit. */
export const AIScheduleEditor = () => {
  const [windows, setWindows] = useState([]);
  const [defaultInterval, setDefaultInterval] = useState(10);
  const [active, setActive] = useState(null);
  const [cooldown, setCooldown] = useState(15);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [sched, guard] = await Promise.all([
        fetch(`${API_URL}/api/ai/schedule`).then(r => r.json()),
        fetch(`${API_URL}/api/ai/notify-guard`).then(r => r.json()),
      ]);
      setWindows(sched.schedule || []);
      setDefaultInterval(sched.default_interval_min ?? 10);
      setActive(sched.active || null);
      setCooldown(guard.cooldown_min ?? 15);
    } catch (e) { /* silent */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const update = (i, patch) => setWindows(w => w.map((x, idx) => idx === i ? { ...x, ...patch } : x));
  const remove = (i) => setWindows(w => w.filter((_, idx) => idx !== i));
  const add = (preset) => setWindows(w => [...w, preset
    ? { ...preset, enabled: true }
    : { from: '09:00', to: '12:00', interval_min: 10, label: '', enabled: true }]);

  const save = async () => {
    setSaving(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ schedule: windows, default_interval_min: Number(defaultInterval) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Speichern fehlgeschlagen');
      setWindows(data.schedule || []);
      setActive(data.active || null);
      toast.success(`Zeitplan gespeichert – aktuell alle ${data.active?.interval_min} min (${data.active?.window})`);
    } catch (e) { toast.error(e.message); } finally { setSaving(false); }
  };

  const saveCooldown = async (value) => {
    setCooldown(value);
    try {
      const res = await fetch(`${API_URL}/api/ai/notify-guard`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ cooldown_min: Number(value) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Fehler');
    } catch (e) { toast.error(e.message); }
  };

  return (
    <div data-testid="ai-schedule-editor">
      <div className="gov-head gov-head-sub">
        <span className="gov-title"><Clock size={14} weight="fill" /> Analyse-Zeitplan der KI</span>
        {active && <span className="gov-meta">aktuell alle {active.interval_min} min · {active.window}</span>}
      </div>
      <p className="gov-hint">
        Lege je Zeitfenster ein eigenes Intervall fest (deutsche Zeit / Europe/Berlin) – z.B. nachts
        alle 30 min, zur US-Eröffnung alle 5 min. Fenster über Mitternacht sind erlaubt; das erste
        passende Fenster gewinnt, sonst gilt das Standard-Intervall. Optional kann pro Fenster ein
        eigenes KI-Modell gewählt werden (z.B. starkes Modell zur US-Eröffnung, günstiges nachts).
      </p>

      {windows.map((w, i) => (
        <div className="gov-sched-row" key={i} data-testid={`schedule-window-${i}`}>
          <input type="time" value={w.from} onChange={e => update(i, { from: e.target.value })}
            data-testid={`schedule-from-${i}`} />
          <span className="gov-sched-sep">bis</span>
          <input type="time" value={w.to} onChange={e => update(i, { to: e.target.value })}
            data-testid={`schedule-to-${i}`} />
          <span className="gov-sched-sep">alle</span>
          <input type="number" min="1" max="360" value={w.interval_min}
            onChange={e => update(i, { interval_min: Number(e.target.value) })}
            data-testid={`schedule-interval-${i}`} />
          <span className="gov-sched-sep">min</span>
          <input type="text" placeholder="Bezeichnung" value={w.label || ''}
            onChange={e => update(i, { label: e.target.value })}
            data-testid={`schedule-label-${i}`} />
          <select value={w.model ? `${w.provider || ''}|${w.model}` : ''}
            onChange={e => {
              const [p, m] = e.target.value ? e.target.value.split('|') : ['', ''];
              update(i, { provider: p, model: m });
            }}
            title="Eigenes KI-Modell nur für dieses Zeitfenster (z.B. starkes Modell zur US-Eröffnung, günstiges nachts). Leer = Haupt-Modell."
            data-testid={`schedule-model-${i}`}>
            <option value="">Standard-KI</option>
            {MODEL_OPTIONS.map(o => (
              <option key={`${o.provider}|${o.model}`} value={`${o.provider}|${o.model}`}>{o.label}</option>
            ))}
          </select>
          <label className="gov-check">
            <input type="checkbox" checked={w.enabled !== false}
              onChange={e => update(i, { enabled: e.target.checked })}
              data-testid={`schedule-enabled-${i}`} />
            <span>aktiv</span>
          </label>
          <button className="ai-lesson-btn danger" onClick={() => remove(i)}
            data-testid={`schedule-remove-${i}`} title="Fenster entfernen">
            <Trash size={14} weight="bold" />
          </button>
        </div>
      ))}

      <div className="gov-actions">
        <button className="gov-btn" onClick={() => add(null)} data-testid="schedule-add">
          <Plus size={13} weight="bold" /> Zeitfenster
        </button>
        {PRESETS.map(p => (
          <button className="gov-btn" key={p.label} onClick={() => add(p)}
            data-testid={`schedule-preset-${p.interval_min}`}>+ {p.label}</button>
        ))}
      </div>

      <div className="gov-rules">
        <label>
          <span>Standard-Intervall (min)</span>
          <input type="number" min="2" max="120" value={defaultInterval}
            onChange={e => setDefaultInterval(e.target.value)}
            data-testid="schedule-default-interval" />
        </label>
        <label>
          <span><BellSlash size={11} /> Telegram-Sperrzeit gleiches Setup (min)</span>
          <input type="number" min="0" max="240" value={cooldown}
            onChange={e => saveCooldown(e.target.value)}
            data-testid="notify-cooldown" />
        </label>
      </div>
      <div className="gov-actions">
        <button className="gov-btn primary" onClick={save} disabled={saving}
          data-testid="schedule-save">
          <FloppyDisk size={14} weight="bold" /> {saving ? 'Speichert…' : 'Zeitplan speichern'}
        </button>
      </div>
    </div>
  );
};

export default AIScheduleEditor;

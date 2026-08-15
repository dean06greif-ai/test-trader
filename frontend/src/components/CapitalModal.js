import React, { useState, useEffect } from 'react';
import { X, Wallet, CheckCircle } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';
import './CapitalModal.css';

const API_URL = process.env.REACT_APP_BACKEND_URL;

const MODES = [
  { id: 'full', title: 'Gesamtes Guthaben', desc: 'Der Bot darf 100% des Guthabens nutzen' },
  { id: 'fixed', title: 'Fester Betrag', desc: 'Der Bot darf maximal diesen USDT-Betrag nutzen' },
  { id: 'percent', title: 'Prozentsatz', desc: 'Der Bot darf einen Anteil des Gesamtguthabens nutzen' },
];

/**
 * CapitalModal
 * Props:
 *  - initialScope: 'live' | 'paper'  (Fallback, wenn kein lockedScope gesetzt ist)
 *  - lockedScope: 'live' | 'paper'   (wenn gesetzt: Tabs werden ausgeblendet,
 *                                     Scope kann nicht mehr gewechselt werden)
 *  - onClose(): schließt Modal
 *  - onSaved(): callback nach erfolgreichem Speichern
 */
export default function CapitalModal({ initialScope = 'live', lockedScope, onClose, onSaved }) {
  const effectiveInitial = lockedScope || initialScope;
  const [scope, setScope] = useState(effectiveInitial);
  const [data, setData] = useState(null);
  const [mode, setMode] = useState('full');
  const [value, setValue] = useState('');
  const [baseBalance, setBaseBalance] = useState('1000');
  const [saving, setSaving] = useState(false);

  // Wenn lockedScope sich ändert, Scope hart synchronisieren
  useEffect(() => {
    if (lockedScope) setScope(lockedScope);
  }, [lockedScope]);

  useEffect(() => {
    fetch(`${API_URL}/api/autotrade/capital`).then(r => r.json()).then(setData).catch(() => {});
  }, []);

  useEffect(() => {
    if (!data) return;
    const a = data.allocation?.[scope] || {};
    setMode(a.mode || 'full');
    setValue(a.value ? String(a.value) : '');
    if (scope === 'paper') setBaseBalance(String(a.base_balance ?? 1000));
  }, [data, scope]);

  const total = scope === 'live'
    ? data?.live_total_balance
    : (parseFloat(baseBalance) || 0);

  const numValue = parseFloat(value) || 0;
  const preview = mode === 'full'
    ? total
    : mode === 'fixed'
      ? (total != null ? Math.min(numValue, total) : numValue)
      : (total != null ? total * Math.min(Math.max(numValue, 0), 100) / 100 : null);

  const validationError = () => {
    if (mode === 'fixed') {
      if (numValue <= 0) return 'Fester Betrag muss größer als 0 sein';
      if (total != null && numValue > total) return `Betrag übersteigt das Gesamtguthaben (${total.toFixed(2)} USDT)`;
    }
    if (mode === 'percent' && (numValue <= 0 || numValue > 100)) return 'Prozentsatz muss zwischen 1 und 100 liegen';
    if (scope === 'paper' && (parseFloat(baseBalance) || 0) <= 0) return 'Simuliertes Guthaben muss größer als 0 sein';
    return null;
  };
  const err = validationError();

  const save = async () => {
    if (err) { toast.error(err); return; }
    setSaving(true);
    try {
      const body = { scope, mode, value: numValue };
      if (scope === 'paper') body.base_balance = parseFloat(baseBalance);
      const res = await fetch(`${API_URL}/api/autotrade/capital`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      });
      const d = await res.json().catch(() => ({}));
      if (res.status === 401) { toast.error('Admin-Login erforderlich'); return; }
      if (!res.ok) { toast.error(d.detail || 'Speichern fehlgeschlagen'); return; }
      toast.success(`Kapital-Zuweisung gespeichert (${scope === 'live' ? 'Live' : 'Paper'})`);
      onSaved && onSaved();
      onClose();
    } catch {
      toast.error('Verbindungsfehler');
    } finally {
      setSaving(false);
    }
  };

  const cur = data?.allocation?.[scope];
  const isLocked = !!lockedScope;
  const scopeLabel = scope === 'live' ? 'LIVE' : 'PAPER';

  return (
    <div className="cap-overlay" onClick={onClose}>
      <div className="cap-modal" onClick={e => e.stopPropagation()} data-testid="capital-modal">
        <div className="cap-header">
          <h3>
            <Wallet size={18} weight="fill" />
            KAPITAL-ZUWEISUNG
            {isLocked && (
              <span className={`cap-scope-badge ${scope}`} data-testid="capital-scope-badge">
                {scopeLabel}
              </span>
            )}
          </h3>
          <button className="cap-close" onClick={onClose} data-testid="capital-modal-close"><X size={20} weight="bold" /></button>
        </div>
        <p className="cap-sub">Lege fest, auf wie viel Guthaben der Bot Zugriff hat. Offene Positionen bleiben bei Änderungen unberührt – das Limit gilt nur für neue Trades.</p>

        {!isLocked && (
          <div className="cap-tabs">
            <button
              className={`cap-tab live ${scope === 'live' ? 'on' : ''}`}
              onClick={() => setScope('live')}
              data-testid="capital-tab-live"
            >LIVE</button>
            <button
              className={`cap-tab paper ${scope === 'paper' ? 'on' : ''}`}
              onClick={() => setScope('paper')}
              data-testid="capital-tab-paper"
            >PAPER</button>
          </div>
        )}

        {scope === 'live' ? (
          <div className="cap-info-row" data-testid="capital-live-total">
            <span>Gesamtguthaben (Bitunix)</span>
            <b className="mono">{total != null ? `${total.toFixed(2)} USDT` : (data?.bitunix_configured ? '—' : 'nicht konfiguriert')}</b>
          </div>
        ) : (
          <label className="cap-field" data-testid="capital-paper-base">
            Simuliertes Gesamtguthaben (USDT)
            <input type="number" min="1" value={baseBalance}
              onChange={e => setBaseBalance(e.target.value)} data-testid="capital-paper-base-input" />
          </label>
        )}

        <div className="cap-modes">
          {MODES.map(m => (
            <label key={m.id} className={`cap-mode ${mode === m.id ? 'on' : ''}`} data-testid={`capital-mode-${m.id}`}>
              <input type="radio" name="cap-mode" checked={mode === m.id} onChange={() => setMode(m.id)} />
              <span className="cap-mode-body">
                <span className="cap-mode-title">{m.title}</span>
                <span className="cap-mode-desc">{m.desc}</span>
              </span>
              {mode === m.id && m.id === 'fixed' && (
                <span className="cap-input-wrap" onClick={e => e.preventDefault()}>
                  <input type="number" min="1" placeholder="z.B. 500" value={value}
                    onClick={e => e.stopPropagation()}
                    onChange={e => setValue(e.target.value)} data-testid="capital-fixed-input" />
                  <em>USDT</em>
                </span>
              )}
              {mode === m.id && m.id === 'percent' && (
                <span className="cap-input-wrap" onClick={e => e.preventDefault()}>
                  <input type="number" min="1" max="100" placeholder="z.B. 25" value={value}
                    onClick={e => e.stopPropagation()}
                    onChange={e => setValue(e.target.value)} data-testid="capital-percent-input" />
                  <em>%</em>
                </span>
              )}
            </label>
          ))}
        </div>

        {err && <div className="cap-error" data-testid="capital-validation-error">{err}</div>}

        <div className="cap-preview" data-testid="capital-preview">
          <div className="cap-preview-item">
            <span>Zugewiesenes Kapital</span>
            <b className="mono">{preview != null ? `${preview.toFixed(2)} USDT` : '—'}</b>
          </div>
          <div className="cap-preview-item">
            <span>Davon belegt (offene Trades)</span>
            <b className="mono">{cur?.used_margin != null ? `${cur.used_margin.toFixed(2)} USDT` : '—'}</b>
          </div>
          <div className="cap-preview-item">
            <span>Frei für neue Trades</span>
            <b className="mono pos">{preview != null ? `${Math.max(preview - (cur?.used_margin || 0), 0).toFixed(2)} USDT` : '—'}</b>
          </div>
        </div>

        <button className="cap-save" onClick={save} disabled={saving || !!err} data-testid="capital-save-btn">
          <CheckCircle size={16} weight="bold" />
          {saving ? 'Speichert...' : 'Speichern'}
        </button>
      </div>
    </div>
  );
}

import React, { useState } from 'react';
import { Warning } from '@phosphor-icons/react';
import { toast } from '../lib/toast';
import { authHeaders } from '../auth';

const API_URL = process.env.REACT_APP_BACKEND_URL;

// Danger-Zone im KI-Setup: setzt den KI-Trader auf 0 zurück (Paper-Trades,
// Signale, Rewards). Erfordert Admin-Login UND erneute Passwort-Eingabe.
const AITraderReset = ({ onDone }) => {
  const [open, setOpen] = useState(false);
  const [pwd, setPwd] = useState('');
  const [busy, setBusy] = useState(false);

  const doReset = async () => {
    if (!pwd) { toast.error('Bitte Admin-Passwort eingeben'); return; }
    setBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/ai/trader/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ password: pwd }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Reset fehlgeschlagen');
      toast.success(`KI-Trader zurückgesetzt: ${data.trades_deleted} Trades, `
        + `${data.signals_deleted} Signale, ${data.rewards_deleted} Rewards gelöscht`);
      setOpen(false); setPwd('');
      onDone && onDone();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  return (
    <div style={{ gridColumn: '1 / -1', borderTop: '1px solid #FF336633', paddingTop: 8, marginTop: 6 }}
      data-testid="ai-trader-reset-row">
      {!open ? (
        <button className="ai-action-btn" style={{ color: '#FF3366', borderColor: '#FF336655' }}
          onClick={() => setOpen(true)} data-testid="ai-trader-reset-open-btn"
          title="Setzt den KI-Trader auf 0 zurück: löscht alle Paper-Trades (inkl. Sammel-Trades), KI-Signale und Belohnungsdaten. Live-Trades bleiben unangetastet. Wird im Audit-Log protokolliert.">
          <Warning size={13} weight="bold" /> KI-Trades auf 0 zurücksetzen…
        </button>
      ) : (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 11, color: '#FF3366' }}>
            Löscht ALLE Paper-Trades, Signale &amp; Rewards des KI-Traders (Live-Trades bleiben).
            Zur Sicherheit Admin-Passwort eingeben:
          </span>
          <input type="password" value={pwd} onChange={e => setPwd(e.target.value)}
            placeholder="Admin-Passwort" style={{ width: 150 }}
            data-testid="ai-trader-reset-password-input" />
          <button className="ai-action-btn" style={{ color: '#FF3366', borderColor: '#FF3366' }}
            onClick={doReset} disabled={busy} data-testid="ai-trader-reset-confirm-btn">
            {busy ? 'Setzt zurück…' : 'Endgültig zurücksetzen'}
          </button>
          <button className="ai-action-btn" onClick={() => { setOpen(false); setPwd(''); }}
            data-testid="ai-trader-reset-cancel-btn">Abbrechen</button>
        </div>
      )}
    </div>
  );
};

export default AITraderReset;

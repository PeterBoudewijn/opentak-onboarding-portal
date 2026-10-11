import { useEffect, useMemo, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import { useAuth } from '../contexts/AuthContext';
import { gameAPI } from '../services/api';
import './Game.css';

const TEAM_LABELS = { red: 'Rood', blue: 'Blauw' };

function formatRemaining(iso) {
  if (!iso) return '';
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return 'verlopen';
  const totalMinutes = Math.floor(diff / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}u ${minutes}m`;
}

function Game() {
  const { isAdmin } = useAuth();
  const [status, setStatus] = useState(null);
  const [access, setAccess] = useState({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState('');
  const [, setTick] = useState(0);

  const admin = isAdmin();

  const loadStatus = async () => {
    try {
      const response = await gameAPI.getStatus();
      setStatus(response.data);
      setError('');
    } catch (err) {
      setError(err.response?.data?.error || 'Game-status kon niet worden geladen.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadStatus();
    const timer = setInterval(() => {
      setTick((value) => value + 1);
      loadStatus();
    }, 30000);
    return () => clearInterval(timer);
  }, []);

  const teams = useMemo(() => status?.teams || [], [status]);

  const generateAccess = async (team) => {
    setBusy(`access-${team}`);
    setError('');
    try {
      const response = await gameAPI.generateAccess(team);
      setAccess((current) => ({ ...current, [team]: response.data }));
      await loadStatus();
    } catch (err) {
      setError(err.response?.data?.error || 'Nieuwe toegang genereren is mislukt.');
    } finally {
      setBusy(null);
    }
  };

  const cleanupTeam = async (team) => {
    const label = TEAM_LABELS[team] || team;
    if (!window.confirm(`Team ${label} afsluiten? QR-toegang wordt ingetrokken en het gameaccount wordt gedeactiveerd.`)) return;
    setBusy(`cleanup-${team}`);
    setError('');
    try {
      await gameAPI.cleanupTeam(team);
      setAccess((current) => ({ ...current, [team]: undefined }));
      await loadStatus();
    } catch (err) {
      setError(err.response?.data?.error || 'Team cleanup is mislukt.');
    } finally {
      setBusy(null);
    }
  };

  const cleanupAll = async () => {
    const confirmation = window.prompt('Typ WISSEN om beide gameteams af te sluiten.');
    if (confirmation !== 'WISSEN') return;
    setBusy('cleanup-all');
    setError('');
    try {
      await gameAPI.cleanupAll();
      setAccess({});
      await loadStatus();
    } catch (err) {
      setError(err.response?.data?.error || 'Cleanup van alle gameteams is mislukt.');
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <div className="game-page"><p>Gameportal laden...</p></div>;

  return (
    <div className="game-page">
      <div className="game-header">
        <div>
          <h1>Airsoft Game</h1>
          <p>Teamtoegang voor ATAK en iTAK. Nieuwe toegang verloopt uiterlijk om 23:00 Europe/Amsterdam.</p>
        </div>
        {admin && (
          <button className="game-danger" disabled={busy !== null} onClick={cleanupAll}>
            Wis alle game-toegang
          </button>
        )}
      </div>

      {error && <div className="game-error">{error}</div>}

      <div className="game-grid">
        {teams.map((team) => {
          const generated = access[team.team];
          const atakQr = generated?.atak?.qrString;
          const itakQr = generated?.itak?.qrString;
          const expiresAt = generated?.expiresAt || team.nextCutoff;
          return (
            <section key={team.team} className={`game-card game-card-${team.team}`}>
              <div className="game-card-title">
                <div>
                  <span className="game-team-badge">TEAM {TEAM_LABELS[team.team]?.toUpperCase()}</span>
                  <h2>{team.username}</h2>
                </div>
                <div className="game-count">{team.eudCount ?? 0} EUD's</div>
              </div>

              <dl className="game-meta">
                <div><dt>OTS-groep</dt><dd>{team.group}</dd></div>
                <div><dt>TAK-kleur</dt><dd>{team.teamColor}</dd></div>
                <div><dt>Volgende cutoff</dt><dd>{expiresAt ? new Date(expiresAt).toLocaleString('nl-NL') : '-'}</dd></div>
                <div><dt>Resterend</dt><dd>{formatRemaining(expiresAt)}</dd></div>
              </dl>

              {!generated && (
                <div className="game-empty">
                  <p>Genereer nieuwe tijdelijke credentials en QR-toegang voor deze speldag.</p>
                  <button disabled={busy !== null} onClick={() => generateAccess(team.team)}>
                    {busy === `access-${team.team}` ? 'Genereren...' : 'Nieuwe toegang genereren'}
                  </button>
                </div>
              )}

              {generated && (
                <>
                  <div className="game-credentials">
                    <div><span>Gebruiker</span><strong>{generated.username}</strong></div>
                    <div><span>Tijdelijk wachtwoord</span><strong className="game-password">{generated.password}</strong></div>
                    <small>Dit wachtwoord wordt alleen in deze respons getoond. Bewaar het niet langer dan nodig.</small>
                  </div>

                  <div className="game-qr-grid">
                    <div className="game-qr">
                      <h3>ATAK</h3>
                      {atakQr ? <QRCodeSVG value={atakQr} size={220} level="M" /> : <p>Geen ATAK QR ontvangen.</p>}
                    </div>
                    <div className="game-qr">
                      <h3>iTAK</h3>
                      {itakQr ? <QRCodeSVG value={itakQr} size={220} level="M" /> : <p>Geen iTAK QR ontvangen.</p>}
                      <small>iTAK gebruikt daarnaast bovenstaande gebruikersnaam en wachtwoord.</small>
                    </div>
                  </div>

                  <button disabled={busy !== null} onClick={() => generateAccess(team.team)}>
                    QR / wachtwoord vernieuwen
                  </button>
                </>
              )}

              {admin && (
                <div className="game-admin-actions">
                  <button className="game-danger" disabled={busy !== null} onClick={() => cleanupTeam(team.team)}>
                    {busy === `cleanup-${team.team}` ? 'Afsluiten...' : `Wis Team ${TEAM_LABELS[team.team]}`}
                  </button>
                  <p className="game-warning">
                    Deze actie trekt credentials in en deactiveert {team.username}. OTS heeft geen ondersteunde DELETE /api/eud-route; historische EUD-records worden daarom nog niet fysiek verwijderd.
                  </p>
                </div>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

export default Game;

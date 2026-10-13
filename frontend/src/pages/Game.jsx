import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { QRCodeSVG } from 'qrcode.react';
import { useAuth } from '../contexts/AuthContext';
import { gameAPI } from '../services/api';
import './Game.css';

const TEAM_LABELS = { red: 'Rood', blue: 'Blauw' };
const ATAK_APP_URL = 'https://play.google.com/store/apps/details?id=com.atakmap.app.civ';
const ITAK_APP_URL = 'https://apps.apple.com/nl/app/itak/id1561656396';

function formatRemaining(iso) {
  if (!iso) return '';
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return 'verlopen';
  const totalMinutes = Math.floor(diff / 60000);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}u ${minutes}m`;
}

function Game({ forcedTeam = null }) {
  const { isAdmin } = useAuth();
  const { team: routeTeam } = useParams();
  const requestedTeam = forcedTeam || routeTeam;
  const [status, setStatus] = useState(null);
  const [access, setAccess] = useState({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState('');
  const [coQr, setCoQr] = useState(null);
  const [coTakAccess, setCoTakAccess] = useState({});
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

  useEffect(() => {
    if (!admin) return;
    gameAPI.getCoQr()
      .then((response) => setCoQr(response.data?.teams || null))
      .catch(() => setCoQr(null));
  }, [admin]);

  const teams = useMemo(() => status?.teams || [], [status]);
  const visibleTeams = useMemo(() => {
    if (!requestedTeam || !['red', 'blue'].includes(requestedTeam)) return teams;
    return teams.filter((team) => team.team === requestedTeam);
  }, [teams, requestedTeam]);
  const requestedTeamDenied = (
    requestedTeam &&
    ['red', 'blue'].includes(requestedTeam) &&
    !teams.some((team) => team.team === requestedTeam)
  );

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

  const loginCoTak = async (team) => {
    const displayName = team === 'red' ? 'CORood' : 'COBlauw';
    setBusy(`co-tak-${team}`);
    setError('');
    try {
      const response = await gameAPI.generateCoTakAccess(team);
      const data = response.data || {};
      setCoTakAccess((current) => ({ ...current, [team]: data }));

      if (!data.takUrl) {
        throw new Error('OpenTAKServer gaf geen ATAK-loginlink terug.');
      }

      // The CO explicitly pressed the button, so hand the same device over to
      // ATAK's custom enrollment URL. If Android returns to the browser, a
      // fallback link remains visible below the button.
      window.location.href = data.takUrl;
    } catch (err) {
      setError(
        err.response?.data?.error ||
        err.message ||
        `Inloggen als ${displayName} op de TAK-server is mislukt.`
      );
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

  const resetCoQr = async (team) => {
    const label = TEAM_LABELS[team] || team;
    const configured = Boolean(coQr?.[team]?.configured && coQr?.[team]?.url);
    const message = configured
      ? `CO ${label} QR-code resetten? De oude CO QR, bestaande CO-login en alle tijdelijke speleraccounts van Team ${label} worden direct ingetrokken.`
      : `CO ${label} QR-code aanmaken? Alleen administrators kunnen deze permanente CO-login QR aanmaken of resetten.`;
    if (!window.confirm(message)) return;

    setBusy(`co-qr-${team}`);
    setError('');
    try {
      const response = await gameAPI.resetCoQr(team);
      setCoQr(response.data?.teams || null);
      setAccess((current) => ({ ...current, [team]: undefined }));
      await loadStatus();

      const failures = response.data?.playerCleanup?.failures || [];
      if (failures.length) {
        setError(
          `CO QR is vernieuwd, maar ${failures.length} tijdelijk speleraccount kon niet volledig worden ingetrokken. Controleer de serverlog.`
        );
      }
    } catch (err) {
      setError(err.response?.data?.error || `CO ${label} QR resetten is mislukt.`);
    } finally {
      setBusy(null);
    }
  };

  const resetAllCoQr = async () => {
    const confirmation = window.prompt(
      'Dit reset beide CO QR-codes en trekt alle tijdelijke speleraccounts van Rood en Blauw in. Typ QR RESET om door te gaan.'
    );
    if (confirmation !== 'QR RESET') return;

    setBusy('co-qr-all');
    setError('');
    try {
      const response = await gameAPI.resetAllCoQr();
      setCoQr(response.data?.teams || null);
      setAccess({});
      await loadStatus();

      const cleanup = response.data?.playerCleanup || {};
      const failures = Object.values(cleanup)
        .flatMap((item) => item?.failures || []);
      if (failures.length) {
        setError(
          `CO QR-codes zijn vernieuwd, maar ${failures.length} tijdelijk speleraccount kon niet volledig worden ingetrokken. Controleer de serverlog.`
        );
      }
    } catch (err) {
      setError(err.response?.data?.error || 'CO QR-codes resetten is mislukt.');
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

      {admin && coQr && (
        <section className="game-card">
          <div className="game-card-title">
            <div>
              <span className="game-team-badge">BEHEER</span>
              <h2>Permanente CO-login QR-codes</h2>
            </div>
          </div>
          <p>
            Alleen een administrator kan deze permanente CO-login QR-codes aanmaken of
            resetten. De QR logt de CO direct in met alleen toegang tot het eigen team.
            Print hem alleen voor de betreffende CO.
          </p>
          <div className="game-qr-grid">
            {['red', 'blue'].map((teamKey) => (
              <div className="game-qr" key={teamKey}>
                <h3>CO {TEAM_LABELS[teamKey]}</h3>
                {coQr?.[teamKey]?.username && (
                  <p>Automatisch inloggen als <strong>{coQr[teamKey].username}</strong></p>
                )}
                {coQr?.[teamKey]?.configured && coQr?.[teamKey]?.url ? (
                  <QRCodeSVG value={coQr[teamKey].url} size={240} level="H" />
                ) : (
                  <p>QR-login is nog niet geconfigureerd.</p>
                )}
                <button
                  className="game-danger"
                  disabled={busy !== null}
                  onClick={() => resetCoQr(teamKey)}
                >
                  {busy === `co-qr-${teamKey}`
                    ? (coQr?.[teamKey]?.configured ? 'Resetten...' : 'Aanmaken...')
                    : (coQr?.[teamKey]?.configured
                      ? `Reset QR CO ${TEAM_LABELS[teamKey]}`
                      : `Maak QR CO ${TEAM_LABELS[teamKey]} aan`)}
                </button>
              </div>
            ))}
          </div>
          <div className="game-admin-actions">
            <button
              className="game-danger"
              disabled={busy !== null}
              onClick={resetAllCoQr}
            >
              {busy === 'co-qr-all' ? 'Resetten...' : 'Reset beide CO QR-codes'}
            </button>
          </div>
          <p className="game-warning">
            Reset maakt de oude CO QR direct ongeldig, blokkeert bestaande CO-toegang via
            die QR en trekt ook alle tijdelijke speleraccounts van het betreffende team in.
            Tijdelijke OTS-users worden verwijderd zodra hun EUD is disconnected. CORood en
            COBLauw blijven onaangetast. Print daarna de nieuwe QR voor de betreffende CO.
          </p>
        </section>
      )}

      {requestedTeamDenied && (
        <div className="game-error">
          Dit account heeft geen toegang tot Team {TEAM_LABELS[requestedTeam] || requestedTeam}.
        </div>
      )}

      {!requestedTeamDenied && visibleTeams.length > 0 && (
        <section className="game-card game-app-downloads">
          <div className="game-card-title">
            <div>
              <span className="game-team-badge">APP INSTALLATIE</span>
              <h2>TAK-app installeren</h2>
            </div>
          </div>
          <p>
            Heeft een speler de TAK-app nog niet? Scan eerst de juiste QR-code om de app
            te installeren. Daarna kan de speler de team-QR hieronder scannen.
          </p>
          <div className="game-qr-grid">
            <div className="game-qr">
              <h3>Android / ATAK-CIV</h3>
              <QRCodeSVG value={ATAK_APP_URL} size={200} level="M" />
              <small>Opent ATAK-CIV in Google Play</small>
            </div>
            <div className="game-qr">
              <h3>Apple / iTAK</h3>
              <QRCodeSVG value={ITAK_APP_URL} size={200} level="M" />
              <small>Opent iTAK in de Apple App Store</small>
            </div>
          </div>
        </section>
      )}

      <div className="game-grid">
        {visibleTeams.map((team) => {
          const generated = access[team.team];
          const atakQr = generated?.atak?.qrString;
          const itakQr = generated?.itak?.qrString;
          const expiresAt = generated?.expiresAt || team.expiresAt;
          return (
            <section key={team.team} className={`game-card game-card-${team.team}`}>
              <div className="game-card-title">
                <div>
                  <span className="game-team-badge">TEAM {TEAM_LABELS[team.team]?.toUpperCase()}</span>
                  <h2>{team.accountLabel || (team.team === 'red' ? 'GameRed' : 'GameBlue')}</h2>
                </div>
                <div className="game-count">{team.eudCount ?? 0} EUD's</div>
              </div>

              <dl className="game-meta">
                <div><dt>OTS-groep</dt><dd>{team.group}</dd></div>
                <div><dt>TAK-kleur</dt><dd>{team.teamColor}</dd></div>
                <div><dt>Volgende cutoff</dt><dd>{expiresAt ? new Date(expiresAt).toLocaleString('nl-NL') : '-'}</dd></div>
                <div><dt>Resterend</dt><dd>{formatRemaining(expiresAt)}</dd></div>
              </dl>

              {team.coTakLogin?.available && (
                <div className="game-co-tak-login">
                  <div>
                    <span className="game-team-badge">CO TAK-LOGIN</span>
                    <h3>{team.coTakLogin.username}</h3>
                    <p>
                      Je bent in het portal ingelogd als <strong>{team.coTakLogin.username}</strong>.
                      Open ATAK op dit toestel en meld het aan op de TAK-server met
                      teamkleur <strong>{team.teamColor}</strong> en Role <strong>HQ</strong>.
                    </p>
                    <p className="game-warning">
                      Meerdere CO&apos;s mogen dit tegelijk gebruiken. Ieder toestel krijgt
                      een eigen clientcertificaat onder dezelfde CO-identiteit.
                    </p>
                  </div>
                  <button
                    className="game-co-tak-button"
                    disabled={busy !== null}
                    onClick={() => loginCoTak(team.team)}
                  >
                    {busy === `co-tak-${team.team}`
                      ? 'TAK-login openen...'
                      : `Inloggen als ${team.coTakLogin.username}`}
                  </button>
                  {coTakAccess[team.team]?.takUrl && (
                    <a className="game-co-tak-fallback" href={coTakAccess[team.team].takUrl}>
                      ATAK opnieuw openen als {team.coTakLogin.username}
                    </a>
                  )}
                </div>
              )}

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
                    <div className="game-qr game-itak-qr">
                      <h3>iPhone / iTAK</h3>
                      {itakQr ? <QRCodeSVG value={itakQr} size={240} level="M" /> : <p>Geen iTAK QR ontvangen.</p>}
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
                    Deze actie trekt de tijdelijke gamecredentials in en deactiveert alle nog actieve sessieaccounts voor dit team. Historische EUD-records blijven in OTS staan, maar hun unieke sessieaccounts worden nooit opnieuw geactiveerd.
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

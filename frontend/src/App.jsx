import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import './styles.css';

const CARD_NAMES = {
  0: 'Spy',
  1: 'Guard',
  2: 'Priest',
  3: 'Baron',
  4: 'Handmaid',
  5: 'Prince',
  6: 'Chancellor',
  7: 'King',
  8: 'Countess',
  9: 'Princess',
};

const CARD_DESC = {
  0: 'ラウンド終了時、唯一のSpy生存者なら+1トークン',
  1: '相手の手札を推測。当たれば脱落',
  2: '相手の手札を覗く',
  3: '相手と手札比較。低い方が脱落',
  4: '次のターン開始まで保護',
  5: '対象の手札を捨てて引き直させる',
  6: '2枚引いて3枚から1枚選択',
  7: '相手と手札を交換',
  8: 'KingかPrinceがあれば必ずプレイ',
  9: 'プレイ/捨てると即脱落',
};

function uuidv4() {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

function getTtw(n) {
  return { 2: 6, 3: 5, 4: 4, 5: 3, 6: 3 }[n] || 6;
}

function tokenDots(current, max) {
  const dots = [];
  for (let i = 0; i < max; i += 1) {
    dots.push(<div key={i} className={`token-dot${i < current ? '' : ' empty'}`} />);
  }
  return dots;
}

function Card({ val, small = false, selected = false, disabled = false, onClick }) {
  const bg = { background: `var(--card-${val})` };
  if (small) {
    return (
      <div className="mini-card" style={bg} title={CARD_NAMES[val]}>
        {val}
      </div>
    );
  }

  const className = [
    'card',
    selected ? 'selected' : '',
    disabled ? 'disabled' : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div
      className={className}
      style={bg}
      onClick={disabled ? undefined : onClick}
      role="button"
      tabIndex={disabled ? -1 : 0}
      onKeyDown={(e) => {
        if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onClick?.();
        }
      }}
    >
      <div className="card-value">{val}</div>
      <div className="card-name">{CARD_NAMES[val]}</div>
      <div className="card-desc">{CARD_DESC[val]}</div>
    </div>
  );
}

export default function App() {
  const [myPlayerId] = useState(() => localStorage.getItem('ll_player_id') || uuidv4());
  const [myRoomId, setMyRoomId] = useState(() => localStorage.getItem('ll_room_id') || '');
  const [myName, setMyName] = useState('');
  const [screen, setScreen] = useState('entry');
  const [entryName, setEntryName] = useState('');
  const [entryError, setEntryError] = useState('');
  const [availableRooms, setAvailableRooms] = useState([]);
  const [roomsLoading, setRoomsLoading] = useState(false);
  const [wsStatus, setWsStatus] = useState('切断');
  const [wsConnected, setWsConnected] = useState(false);
  const [gameState, setGameState] = useState(null);
  const [selectedCardIndex, setSelectedCardIndex] = useState(null);
  const [gameLog, setGameLog] = useState([]);
  const [toast, setToast] = useState({ message: '', visible: false });
  const [overlay, setOverlay] = useState(null);
  const [roundEndMsg, setRoundEndMsg] = useState(null);
  const [gameOverNames, setGameOverNames] = useState([]);

  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const pingTimerRef = useRef(null);
  const toastTimerRef = useRef(null);
  const pendingMessageRef = useRef(null);
  const myRoomIdRef = useRef(myRoomId);
  const myNameRef = useRef(myName);
  const gameStateRef = useRef(gameState);

  useEffect(() => {
    localStorage.setItem('ll_player_id', myPlayerId);
  }, [myPlayerId]);

  useEffect(() => {
    myRoomIdRef.current = myRoomId;
    if (myRoomId) localStorage.setItem('ll_room_id', myRoomId);
  }, [myRoomId]);

  useEffect(() => {
    myNameRef.current = myName;
  }, [myName]);

  useEffect(() => {
    gameStateRef.current = gameState;
  }, [gameState]);

  const showToast = useCallback((message, duration = 3000) => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    setToast({ message, visible: true });
    toastTimerRef.current = setTimeout(() => {
      setToast((prev) => ({ ...prev, visible: false }));
    }, duration);
  }, []);

  const addLog = useCallback((message) => {
    setGameLog((prev) => [...prev, message]);
  }, []);

  const fetchRooms = useCallback(async () => {
    setRoomsLoading(true);
    try {
      const response = await fetch('/api/rooms', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      setAvailableRooms(Array.isArray(payload.rooms) ? payload.rooms : []);
    } catch {
      setAvailableRooms([]);
    } finally {
      setRoomsLoading(false);
    }
  }, []);

  const send = useCallback((payload) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }, []);

  const handleRoundEnd = useCallback(
    (msg, currentState) => {
      setRoundEndMsg(msg);

      const winnerNames = (msg.round_winner_ids || []).map((wid) => {
        const u = (msg.token_updates || []).find((t) => t.player_id === wid);
        return u?.name || wid;
      });

      if (currentState) {
        const hostId = currentState.players?.[0]?.player_id;
        if (hostId !== currentState.your_player_id) {
          // no-op: button visibility is handled in render
        }
      }

      if (winnerNames.length > 0) {
        addLog(`${winnerNames.join(', ')} がラウンド勝利`);
      }
      setScreen('round_end');
    },
    [addLog]
  );

  const handleGameOver = useCallback(
    (msg, currentState) => {
      if (!currentState) return;
      const winnerNames = (msg.winner_ids || []).map((wid) => {
        const p = currentState.players.find((player) => player.player_id === wid);
        return p?.name || wid;
      });
      setGameOverNames(winnerNames);
      setTimeout(() => setScreen('game_over'), 2000);
    },
    []
  );

  const connect = useCallback(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState <= 1) return;

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${proto}://${window.location.host}/ws`);
    wsRef.current = socket;

    socket.onopen = () => {
      setWsStatus('接続済');
      setWsConnected(true);
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      if (pendingMessageRef.current) {
        send(pendingMessageRef.current);
        pendingMessageRef.current = null;
        return;
      }

      if (myRoomIdRef.current && myNameRef.current) {
        send({
          type: 'join',
          player_id: myPlayerId,
          name: myNameRef.current,
          room_id: myRoomIdRef.current,
        });
      }
    };

    socket.onclose = () => {
      setWsStatus('切断');
      setWsConnected(false);
      reconnectTimerRef.current = setTimeout(connect, 2000);
    };

    socket.onerror = () => {};

    socket.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      switch (msg.type) {
        case 'pong':
          break;
        case 'room_created':
          setMyRoomId(msg.room_id);
          setScreen('lobby');
          break;
        case 'player_joined':
          break;
        case 'state_update': {
          setGameState(msg);
          gameStateRef.current = msg;
          const phase = msg.phase;
          if (phase === 'lobby') setScreen('lobby');
          if (phase === 'playing') setScreen('game');
          break;
        }
        case 'game_event': {
          const d = msg.data;
          if (d?.description) addLog(d.description);
          if (msg.event_type === 'priest_reveal' && d?.player_id === myPlayerId) {
            const cardNames = (d.target_hand || [])
              .map((v) => `${CARD_NAMES[v]}(${v})`)
              .join(', ');
            showToast(`${d.target_name} の手札: ${cardNames}`, 5000);
            addLog(`→ あなたは ${d.target_name} の手札を確認: ${cardNames}`);
          }
          break;
        }
        case 'await_input': {
          if (msg.acting_player_id && msg.acting_player_id !== myPlayerId) break;

          if (msg.action_type === 'select_target') {
            showToast('対象プレイヤーを選んでください');
            const candidates =
              msg.candidates ||
              gameStateRef.current?.pending_action?.candidate_target_ids ||
              [];
            if (candidates.includes(myPlayerId)) {
              setOverlay({
                type: 'target',
                cardPlayed: msg.card_played,
                candidates,
              });
            }
          }

          if (msg.action_type === 'guard_guess') {
            setOverlay({
              type: 'guard',
              targetName: msg.target_name,
            });
          }

          if (msg.action_type === 'chancellor_return') {
            setOverlay({
              type: 'chancellor',
              hand: msg.chancellor_hand || [],
              keptIndex: null,
              orderIndices: [],
            });
          }
          break;
        }
        case 'round_end':
          handleRoundEnd(msg, gameStateRef.current);
          break;
        case 'game_over':
          handleGameOver(msg, gameStateRef.current);
          break;
        case 'error':
          showToast(`エラー: ${msg.message}`);
          setEntryError(msg.message || 'エラーが発生しました');
          break;
        default:
          break;
      }
    };
  }, [
    addLog,
    handleGameOver,
    handleRoundEnd,
    myPlayerId,
    send,
    showToast,
  ]);

  useEffect(() => {
    connect();

    pingTimerRef.current = setInterval(() => {
      send({ type: 'ping' });
    }, 30000);

    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (pingTimerRef.current) clearInterval(pingTimerRef.current);
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
      if (wsRef.current && wsRef.current.readyState <= 1) wsRef.current.close();
    };
  }, [connect, send]);

  useEffect(() => {
    if (screen !== 'entry') return undefined;
    fetchRooms();
    const timer = setInterval(fetchRooms, 3000);
    return () => clearInterval(timer);
  }, [fetchRooms, screen]);

  const me = useMemo(() => {
    if (!gameState) return null;
    return gameState.players.find((p) => p.player_id === gameState.your_player_id) || null;
  }, [gameState]);

  const isHost = useMemo(() => {
    if (!gameState) return false;
    return gameState.players?.[0]?.player_id === gameState.your_player_id;
  }, [gameState]);

  useEffect(() => {
    if (!me) {
      setSelectedCardIndex(null);
      return;
    }
    if (selectedCardIndex !== null && selectedCardIndex >= me.hand.length) {
      setSelectedCardIndex(null);
    }
  }, [me, selectedCardIndex]);

  const playCard = () => {
    if (!myRoomId || selectedCardIndex === null) return;
    send({
      type: 'play_card',
      room_id: myRoomId,
      player_id: myPlayerId,
      card_index: selectedCardIndex,
    });
    setSelectedCardIndex(null);
  };

  const selectTarget = (targetId) => {
    if (!myRoomId) return;
    send({
      type: 'select_target',
      room_id: myRoomId,
      player_id: myPlayerId,
      target_id: targetId,
    });
    setOverlay(null);
  };

  const submitGuardGuess = (value) => {
    if (!myRoomId) return;
    send({
      type: 'guard_guess',
      room_id: myRoomId,
      player_id: myPlayerId,
      guessed_value: value,
    });
    setOverlay(null);
  };

  const submitChancellor = () => {
    if (!overlay || overlay.type !== 'chancellor' || !myRoomId) return;
    if (overlay.keptIndex === null) return;

    const keptCard = overlay.hand[overlay.keptIndex];
    const remainingIndices = overlay.hand
      .map((_, idx) => idx)
      .filter((idx) => idx !== overlay.keptIndex);

    const orderIndices =
      overlay.orderIndices.length === remainingIndices.length
        ? overlay.orderIndices
        : remainingIndices;

    const bottomOrder = orderIndices.map((idx) => overlay.hand[idx]);

    send({
      type: 'chancellor_return',
      room_id: myRoomId,
      player_id: myPlayerId,
      kept_card: keptCard,
      bottom_order: bottomOrder,
    });

    setOverlay(null);
  };

  const onCreateRoom = () => {
    const name = entryName.trim();
    if (!name) {
      setEntryError('名前を入力してください');
      return;
    }
    setMyName(name);
    setEntryError('');

    const message = { type: 'create_room', player_id: myPlayerId, name };
    if (!send(message)) {
      pendingMessageRef.current = message;
      connect();
    }
  };

  const onJoinRoom = (roomId) => {
    const name = entryName.trim();
    if (!name) {
      setEntryError('名前を入力してください');
      return;
    }

    setMyName(name);
    setMyRoomId(roomId);
    setEntryError('');

    const message = {
      type: 'join',
      player_id: myPlayerId,
      name,
      room_id: roomId,
    };

    if (!send(message)) {
      pendingMessageRef.current = message;
      connect();
    }
  };

  const onStartGame = () => {
    if (!myRoomId) return;
    send({ type: 'start_game', room_id: myRoomId, player_id: myPlayerId });
  };

  const onNextRound = () => {
    if (!myRoomId) return;
    send({ type: 'next_round', room_id: myRoomId, player_id: myPlayerId });
  };

  const onBackLobby = () => {
    setMyRoomId('');
    localStorage.removeItem('ll_room_id');
    setGameState(null);
    setRoundEndMsg(null);
    setGameOverNames([]);
    setGameLog([]);
    setScreen('entry');
  };

  const renderOverlay = () => {
    if (!overlay || !gameState) return null;

    if (overlay.type === 'target') {
      return (
        <div className="action-overlay active" id="action-overlay">
          <div className="action-box" id="action-box">
            <h3 id="action-title">{CARD_NAMES[overlay.cardPlayed]} の対象選択</h3>
            <p id="action-desc">対象を選んでください（自分を含む）</p>
            <div className="action-buttons" id="action-buttons">
              {overlay.candidates.map((cid) => {
                const p = gameState.players.find((pl) => pl.player_id === cid);
                if (!p) return null;
                const isSelf = cid === myPlayerId;
                return (
                  <button
                    key={cid}
                    className={isSelf ? 'btn' : 'btn-outline'}
                    onClick={() => selectTarget(cid)}
                  >
                    {isSelf ? `自分 (${p.name})` : p.name}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      );
    }

    if (overlay.type === 'guard') {
      return (
        <div className="action-overlay active" id="action-overlay">
          <div className="action-box" id="action-box">
            <h3 id="action-title">Guard: {overlay.targetName} の手札を推測</h3>
            <p id="action-desc">Guard以外のカードを選んでください</p>
            <div className="action-buttons" id="action-buttons">
              {Object.keys(CARD_NAMES)
                .map(Number)
                .filter((v) => v !== 1)
                .map((v) => (
                  <button
                    key={v}
                    className="btn-outline"
                    style={{ background: `var(--card-${v})`, border: 'none', color: '#fff' }}
                    onClick={() => submitGuardGuess(v)}
                  >
                    {v} {CARD_NAMES[v]}
                  </button>
                ))}
            </div>
          </div>
        </div>
      );
    }

    if (overlay.type === 'chancellor') {
      const remainingIndices = overlay.hand
        .map((_, idx) => idx)
        .filter((idx) => idx !== overlay.keptIndex);
      const orderDone =
        overlay.keptIndex !== null && overlay.orderIndices.length === remainingIndices.length;

      return (
        <div className="action-overlay active" id="action-overlay">
          <div className="action-box" id="action-box">
            <h3 id="action-title">Chancellor: 手札を選ぶ</h3>
            <p id="action-desc">
              3枚の中から1枚を手元に残し、残り2枚をデッキ下に戻す順番を選んでください
            </p>
            <div className="action-buttons" id="action-buttons">
              <div className="chancellor-cards">
                {overlay.hand.map((val, idx) => (
                  <Card
                    key={`${val}-${idx}`}
                    val={val}
                    selected={overlay.keptIndex === idx}
                    onClick={() =>
                      setOverlay((prev) =>
                        prev && prev.type === 'chancellor'
                          ? { ...prev, keptIndex: idx, orderIndices: [] }
                          : prev
                      )
                    }
                  />
                ))}
              </div>

              <p id="chancellor-order-label" style={{ marginTop: 8 }}>
                {overlay.keptIndex === null
                  ? '手札に残すカードをクリックしてください'
                  : remainingIndices.length <= 1
                    ? `残り: ${remainingIndices
                        .map((i) => CARD_NAMES[overlay.hand[i]])
                        .join(', ')} をデッキ下に戻します`
                    : orderDone
                      ? `戻す順: ${overlay.orderIndices
                          .map((i) => CARD_NAMES[overlay.hand[i]])
                          .join(' → ')} (下から)`
                      : 'デッキ下に戻す順番を選んでください（先にクリックしたカードが一番下）'}
              </p>

              {overlay.keptIndex !== null && remainingIndices.length > 1 && (
                <div
                  id="chancellor-order-btns"
                  style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 8 }}
                >
                  {remainingIndices.map((idx) => {
                    const selected = overlay.orderIndices.includes(idx);
                    return (
                      <button
                        key={`order-${idx}`}
                        className="btn-outline"
                        disabled={selected}
                        style={{ opacity: selected ? 0.5 : 1 }}
                        onClick={() =>
                          setOverlay((prev) => {
                            if (!prev || prev.type !== 'chancellor') return prev;
                            if (prev.orderIndices.includes(idx)) return prev;
                            return { ...prev, orderIndices: [...prev.orderIndices, idx] };
                          })
                        }
                      >
                        {overlay.hand[idx]} {CARD_NAMES[overlay.hand[idx]]}
                      </button>
                    );
                  })}
                </div>
              )}

              <button
                className="btn"
                disabled={
                  overlay.keptIndex === null ||
                  (remainingIndices.length > 1 && !orderDone)
                }
                onClick={submitChancellor}
              >
                確定
              </button>
            </div>
          </div>
        </div>
      );
    }

    return null;
  };

  const isPlaying = screen === 'game' && gameState && me;
  const tokensToWin = gameState ? getTtw(gameState.players.length) : 6;

  let isMyTurn = false;
  let hasPendingTarget = false;
  let waitingAction = false;
  let countessForced = false;
  let currentPlayer = null;

  if (isPlaying) {
    isMyTurn = gameState.current_player_id === me.player_id;
    const pa = gameState.pending_action;
    hasPendingTarget =
      pa && pa.phase === 'await_target' && pa.acting_player_id === me.player_id;
    waitingAction = pa && pa.phase !== 'await_target';

    const hasCountess = me.hand.includes(8);
    const hasKingOrPrince = me.hand.includes(7) || me.hand.includes(5);
    countessForced = hasCountess && hasKingOrPrince;

    currentPlayer = gameState.players.find((p) => p.player_id === gameState.current_player_id);
  }

  return (
    <>
      <div id="screen-entry" className={`screen ${screen === 'entry' ? 'active' : ''}`}>
        <h1>♥ Love Letter</h1>
        <p className="subtitle">2〜6人用カードゲーム</p>
        <div className="entry-form">
          <input
            id="name-input"
            type="text"
            placeholder="あなたの名前"
            maxLength={20}
            autoComplete="off"
            value={entryName}
            onChange={(e) => setEntryName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onCreateRoom();
            }}
          />
          <button className="btn" id="btn-create" onClick={onCreateRoom}>
            部屋を作る
          </button>
          <div className="room-list-header">
            <span>公開ルーム一覧</span>
            <button className="btn-outline btn-small" onClick={fetchRooms}>
              更新
            </button>
          </div>
          <div className="room-list">
            {availableRooms.length === 0 ? (
              <div className="room-empty">
                {roomsLoading ? 'ルーム一覧を読み込み中...' : '入室可能なルームはありません'}
              </div>
            ) : (
              availableRooms.map((room) => (
                <div key={room.room_id} className="room-item">
                  <div className="room-meta">
                    <div className="room-id">{room.room_id}</div>
                    <div className="room-detail">
                      ホスト: {room.host_name} / {room.player_count}人
                    </div>
                  </div>
                  <button
                    className="btn-outline btn-small"
                    disabled={!entryName.trim()}
                    onClick={() => onJoinRoom(room.room_id)}
                  >
                    参加
                  </button>
                </div>
              ))
            )}
          </div>
          <div className="error-msg" id="entry-error">
            {entryError}
          </div>
        </div>
      </div>

      <div id="screen-lobby" className={`screen ${screen === 'lobby' ? 'active' : ''}`}>
        <h2>ロビー</h2>
        <div className="room-code" id="lobby-room-code">
          {myRoomId || '----'}
        </div>
        <p style={{ color: 'var(--text-dim)', fontSize: '0.85rem' }}>
          このコードを仲間に教えてください
        </p>

        <ul className="player-list" id="lobby-player-list">
          {(gameState?.players || []).map((p, i) => (
            <li key={p.player_id}>
              {i === 0 && <span className="crown">♛ </span>}
              {p.name}
            </li>
          ))}
        </ul>

        <button
          className="btn"
          id="btn-start"
          style={{ minWidth: 180, display: isHost ? '' : 'none' }}
          disabled={(gameState?.players?.length || 0) < 2}
          onClick={onStartGame}
        >
          ゲーム開始
        </button>

        <p className="lobby-hint" id="lobby-hint">
          {(gameState?.players?.length || 0) < 2
            ? 'あと1人以上必要です'
            : `${gameState?.players?.length || 0}人参加中`}
        </p>
      </div>

      <div id="screen-game" className={`screen ${screen === 'game' ? 'active' : ''}`}>
        <div className="game-header">
          <div className="deck-info">
            デッキ: <span id="deck-count">{gameState?.deck_count ?? '?'}</span>枚
          </div>
          <div id="face-up-area" className="face-up-cards">
            {(gameState?.face_up_cards || []).length > 0 && (
              <span style={{ fontSize: '0.8rem', color: 'var(--text-dim)' }}>表向き除外:</span>
            )}
            {(gameState?.face_up_cards || []).map((v, idx) => (
              <Card key={`fu-${idx}`} val={v} small />
            ))}
          </div>
          <div
            id="tokens-to-win-info"
            style={{ fontSize: '0.8rem', color: 'var(--text-dim)', marginLeft: 'auto' }}
          >
            勝利条件: {tokensToWin}トークン
          </div>
        </div>

        <div className="opponents-area" id="opponents-area">
          {(isPlaying ? gameState.players : [])
            .filter((p) => p.player_id !== me.player_id)
            .map((p) => {
              const pa = gameState.pending_action;
              const isCandidate =
                pa &&
                pa.phase === 'await_target' &&
                pa.acting_player_id === me.player_id &&
                pa.candidate_target_ids.includes(p.player_id);

              let statusText = '';
              if (p.eliminated) statusText = '💀 脱落';
              else if (p.protected) statusText = '🛡 保護中';

              const classes = [
                'opponent-card',
                p.player_id === gameState.current_player_id ? 'current-turn' : '',
                p.eliminated ? 'eliminated' : '',
                p.protected ? 'protected' : '',
                isCandidate ? 'target-candidate' : '',
              ]
                .filter(Boolean)
                .join(' ');

              return (
                <div
                  key={p.player_id}
                  className={classes}
                  data-player-id={p.player_id}
                  onClick={isCandidate ? () => selectTarget(p.player_id) : undefined}
                >
                  <div className="opp-name">
                    {p.name}
                    {p.player_id === gameState.players[0]?.player_id ? ' ♛' : ''}
                  </div>
                  <div className="opp-tokens">🏆 {p.tokens}</div>
                  <div className="token-dots">{tokenDots(p.tokens, tokensToWin)}</div>
                  <div className="opp-status">
                    {statusText} {p.hand_count > 0 && !p.eliminated ? '🃏' : ''}
                  </div>
                  <div className="opp-discard">
                    {p.discard_pile.map((v, idx) => (
                      <Card key={`${p.player_id}-d-${idx}`} val={v} small />
                    ))}
                  </div>
                </div>
              );
            })}
        </div>

        <div className="game-log-area" id="game-log">
          {gameLog.map((entry, idx) => (
            <div key={`log-${idx}`} className="log-entry">
              {entry}
            </div>
          ))}
        </div>

        <div className="my-area">
          <div className="my-info">
            <div className="my-name" id="my-name">
              {isPlaying
                ? `${me.name}${me.player_id === gameState.players[0]?.player_id ? ' ♛' : ''}${
                    isMyTurn ? ' ← あなたのターン' : ''
                  }`
                : '-'}
            </div>
            <div className="my-tokens" id="my-tokens">
              🏆 {isPlaying ? me.tokens : 0}
            </div>
            <div className="token-dots" id="my-token-dots">
              {isPlaying ? tokenDots(me.tokens, tokensToWin) : null}
            </div>
            <div style={{ fontSize: '0.75rem', color: 'var(--text-dim)', marginTop: 6 }}>捨て札:</div>
            <div className="my-discard" id="my-discard">
              {isPlaying &&
                me.discard_pile.map((v, idx) => <Card key={`my-d-${idx}`} val={v} small />)}
            </div>
          </div>

          <div className="hand-area" id="hand-area">
            {isPlaying &&
              me.hand.map((val, idx) => {
                const canPlay = isMyTurn && !waitingAction && !hasPendingTarget;
                const countessDisabled = countessForced && val !== 8;
                const disabled = !canPlay || countessDisabled;
                return (
                  <Card
                    key={`my-h-${idx}`}
                    val={val}
                    selected={selectedCardIndex === idx && !disabled}
                    disabled={disabled}
                    onClick={() =>
                      setSelectedCardIndex((prev) => (prev === idx ? null : idx))
                    }
                  />
                );
              })}
          </div>

          <div className="play-btn-area">
            <button
              className="btn"
              id="btn-play-card"
              disabled={
                !isPlaying ||
                !isMyTurn ||
                waitingAction ||
                hasPendingTarget ||
                selectedCardIndex === null
              }
              onClick={playCard}
            >
              プレイ
            </button>
          </div>
        </div>

        <div className="status-bar">
          <span id="turn-indicator">
            {!isPlaying
              ? '-'
              : isMyTurn
                ? 'あなたのターン'
                : `${currentPlayer?.name || '?'} のターン`}
          </span>
          <span id="ws-status" className={wsConnected ? 'connected' : 'disconnected'}>
            {wsStatus}
          </span>
        </div>
      </div>

      <div
        id="screen-round-end"
        className={`screen ${screen === 'round_end' ? 'active' : ''}`}
      >
        <h2 id="round-result-title">
          {(() => {
            const winnerNames = (roundEndMsg?.round_winner_ids || []).map((wid) => {
              const u = (roundEndMsg?.token_updates || []).find((t) => t.player_id === wid);
              return u?.name || wid;
            });
            return winnerNames.length > 0
              ? `${winnerNames.join(', ')} がラウンド勝利！`
              : 'ラウンド終了';
          })()}
        </h2>

        <div className="revealed-hands" id="revealed-hands">
          {(roundEndMsg?.revealed_hands || []).map((r) => {
            const winner = roundEndMsg?.round_winner_ids?.includes(r.player_id);
            const cardNames = (r.hand || []).map((v) => `${v} ${CARD_NAMES[v]}`).join(', ');
            return (
              <div key={r.player_id} className={`revealed-hand${winner ? ' winner' : ''}`}>
                <div className="r-name">{r.name}</div>
                <div>{cardNames || '（脱落）'}</div>
              </div>
            );
          })}
        </div>

        <div className="token-board" id="token-board">
          {(roundEndMsg?.token_updates || []).map((u) => (
            <div key={u.player_id} className="token-entry">
              {u.name}: <span>{u.tokens}</span>
            </div>
          ))}
        </div>

        <div id="spy-bonus-msg" style={{ color: 'var(--gold)', fontSize: '0.9rem' }}>
          {(() => {
            if (!roundEndMsg?.spy_bonus_id) return '';
            const winner = (roundEndMsg?.token_updates || []).find(
              (u) => u.player_id === roundEndMsg.spy_bonus_id
            );
            return `🕵️ ${winner?.name || roundEndMsg.spy_bonus_id} がSpyボーナス +1トークンを獲得！`;
          })()}
        </div>

        <button
          className="btn"
          id="btn-next-round"
          style={{ minWidth: 200, display: isHost ? '' : 'none' }}
          onClick={onNextRound}
        >
          次のラウンド
        </button>
      </div>

      <div
        id="screen-game-over"
        className={`screen ${screen === 'game_over' ? 'active' : ''}`}
      >
        <h2>ゲーム終了！</h2>
        <div className="winner-display" id="game-over-winners">
          <p style={{ color: 'var(--gold)', fontSize: '1.5rem' }}>{gameOverNames.join(', ')}</p>
          <p>おめでとうございます！</p>
        </div>
        <button className="btn" id="btn-back-lobby" style={{ minWidth: 200 }} onClick={onBackLobby}>
          ロビーに戻る
        </button>
      </div>

      {renderOverlay()}

      <div className={`toast ${toast.visible ? '' : 'hidden'}`} id="toast">
        {toast.message}
      </div>
    </>
  );
}

/* mRender GUI の表示言語切替（日本語 = ソース文言、英語 = この辞書で置換）。
 *
 * 方針:
 *   - ソースコード中の文言は従来どおり日本語のまま。英語表示はこの辞書で
 *     「日本語 → 英語」に置き換える（キー = 日本語の原文）。辞書に無い文言は
 *     日本語のまま出る（落ちない）。
 *   - 言語の決定: URL の ?lang=ja|en > localStorage 'mrender.lang'（ja / en /
 *     auto）> auto = ブラウザ（= OS）の言語が日本語なら ja、それ以外は en。
 *   - 静的 HTML は I18N.translateDom() でテキストノードと title / placeholder /
 *     aria-label / alt 属性を置換。JS が生成する文言は I18N.t(原文, 変数) を通す。
 *     サーバーが返すフォームスキーマ（label / help / title / desc）は
 *     I18N.translateSchema() で /api/state 受信直後に置換する。
 *   - 置換は表示時のみ。フォーム値・プリセット名・YAML キー等の内部状態は触らない。
 *   - node からも読める（tests/test_gui_i18n.py が辞書のカバレッジを検査する）。
 */
(function (global) {
  'use strict';

  const DICT = {
    /* ---- ヘッダ・シナリオバー ---- */
    'mRender GUI — 衛星軌道レンダリング': 'mRender GUI — Satellite Orbit Rendering',
    '衛星軌道・近接撮像・望遠鏡観測': 'Satellite orbits · proximity imaging · telescope observation',
    'レンダリング（バッチ・ライブプレビュー共通）を実行するマシン。リモートのジョブ結果は runs/ に自動ミラーされる':
      'Machine that runs rendering (batch and live preview). Remote job results are mirrored to runs/ automatically',
    '実行マシン': 'Machine',
    'テーマ': 'Theme',
    'ダーク': 'Dark',
    'ライト': 'Light',
    '言語': 'Language',
    'auto = ブラウザ（OS）の言語に従う': 'auto = follow the browser (OS) language',
    '自動': 'Auto',
    'AI アシスタントを開く / 閉じる': 'Open / close the AI assistant',
    'シナリオ': 'Scenario',
    'モデル確認': 'Model viewer',
    'ターゲット': 'Target',
    'プリセット': 'Preset',
    '（選択して読み込み）': '(select to load)',
    '再読込': 'Reload',
    'chief オブジェクト': 'chief object',
    'chief = Hill原点 [0, 0, 0] km': 'chief = Hill origin [0, 0, 0] km',
    'deputy オブジェクト': 'deputy object',
    'カメラ': 'Camera',
    '相対軌道のカメラ': 'Relative-orbit camera',
    'chief → deputy（追尾）': 'chief → deputy (tracking)',
    'chief fixed（機体固定）': 'chief fixed (body-fixed)',
    'deputy → chief（追尾）': 'deputy → chief (tracking)',
    'deputy fixed（機体固定）': 'deputy fixed (body-fixed)',
    '外部カメラ（手動）': 'External camera (manual)',
    'fixed は機体姿勢に追従する固定視線。取付位置・方向は詳細の「カメラ」で変更できます。':
      'fixed keeps a line of sight that follows the body attitude. Mount position and direction can be changed under “Camera” in the details.',
    '全体を表示': 'Fit view',
    'X：赤': 'X: red',
    'Y：緑': 'Y: green',
    'Z：青': 'Z: blue',
    'モデル中心を原点として表示。軸方向は元モデルの座標系と同じです。':
      'Shown with the model center at the origin. Axes match the original model’s coordinate system.',
    '視点': 'View',
    '外部・慣性固定': 'External, inertial-fixed',
    '外部・衛星追従': 'External, chase',
    '機載カメラ': 'Onboard camera',
    '地球中心': 'Earth-centered',
    '観測方法': 'Observation method',
    '望遠鏡像＋光度曲線': 'Telescope image + light curve',
    '光度曲線の簡易計算': 'Quick light-curve calculation',
    '連番画像から動画も作成': 'Also make a video from the frames',
    'その他の実行モード': 'Other run modes',
    '実行モード': 'Run mode',
    '▶ レンダリング開始': '▶ Start rendering',
    '1フレームプレビュー': 'Single-frame preview',
    'YAML 表示': 'Show YAML',
    'プリセット保存': 'Save preset',
    'ドラッグでサイドバー幅を変更（ダブルクリックでリセット）': 'Drag to resize the sidebar (double-click to reset)',

    /* ---- モデル確認 / ライブプレビュー パネル ---- */
    '— ブラウザで直接表示': '— shown directly in the browser',
    'ドラッグ：回転 / ホイール：拡大縮小 / Shift＋ドラッグ：移動 / ダブルクリック：全体表示':
      'Drag: rotate / Wheel: zoom / Shift+drag: pan / Double-click: fit',
    '材質は形状確認用の近似表示です。物理レンダリングとは見え方が異なります。':
      'Materials are approximated for shape checking and differ from the physically based render.',
    'ライブプレビュー': 'Live preview',
    'ライブプレビュー ON': 'Live preview ON',
    'ライブプレビュー OFF': 'Live preview OFF',
    'camera_origin / camera_target / camera_up の上書きを消して view_mode 既定のカメラに戻す':
      'Clear the camera_origin / camera_target / camera_up overrides and return to the view_mode default camera',
    'カメラ上書き解除': 'Clear camera override',
    '常駐 Mitsuba ワーカーを終了する': 'Shut down the resident Mitsuba worker',
    'ワーカー停止': 'Stop worker',
    '左ドラッグ=オービット / ホイール=ドリー / Shift+ドラッグ=パン': 'Left drag = orbit / Wheel = dolly / Shift+drag = pan',
    '再試行': 'Retry',
    '解像度': 'Resolution',
    'リファイン': 'Refine',
    'ドラッグ・ホイール・タイムライン操作の最中だけ、大気/雲/夜光/星空を切って低バウンスで描きます（手を離すと通常品質で描き直します）':
      'While dragging, scrolling or scrubbing the timeline, render with atmosphere/clouds/night lights/stars off and fewer bounces (re-rendered at normal quality on release)',
    '簡易操作': 'Fast interaction',
    'ワーカー起動中…': 'Starting worker…',
    'ライブ表示はシーン画像のみです。光度曲線 CSV は本番実行（レンダリング開始）で出力されます。':
      'The live view shows the scene image only. The light-curve CSV is written by a full run (Start rendering).',
    '再生 / 一時停止': 'Play / pause',
    'フレーム': 'Frame',
    '太陽 方位': 'Sun azimuth',
    '太陽 仰角': 'Sun elevation',
    '太陽スライダーは「太陽光の進行方向」ベクトル欄と双方向に同期します（Y 軸を上とした方位/仰角）。':
      'The Sun sliders sync both ways with the “Sunlight direction of travel” vector field (azimuth/elevation with +Y up).',
    'sun_angle 欄を空にして天文学的な自動計算へ戻す': 'Clear the sun_angle field and return to astronomical auto-calculation',
    '自動に戻す': 'Back to auto',
    '「太陽方位 [deg]」欄と同期します。空欄＝天文学的自動計算（自動）。':
      'Synced with the “Sun ecliptic longitude [deg]” field. Empty = astronomical auto-calculation (Auto).',
    '実行中 / 完了ジョブ': 'Running / finished jobs',
    'まだジョブがありません。左のフォームで設定して「レンダリング開始」を押してください。':
      'No jobs yet. Configure the form on the left and press “Start rendering”.',
    '過去のラン (runs/)': 'Past runs (runs/)',

    /* ---- AI drawer ---- */
    '🤖 AI アシスタント': '🤖 AI assistant',
    '実行中…': 'Running…',
    '閉じる (Esc)': 'Close (Esc)',
    'ON にすると、送信時に現在のフォーム内容を YAML 化して一緒に渡します':
      'When ON, the current form is converted to YAML and sent along with the message',
    '現在の GUI 設定を渡す': 'Send current GUI settings',
    'AI への指示… (⌘/Ctrl+Enter で送信)': 'Instructions for the AI…  (⌘/Ctrl+Enter to send)',
    '送信': 'Send',
    'このセッションで使うモデル（未指定なら CLI 設定値）': 'Model for this session (CLI setting if empty)',
    'デフォルト (settings)': 'Default (settings)',
    'reasoning effort（未指定なら CLI 設定値）': 'Reasoning effort (CLI setting if empty)',
    'デフォルト': 'Default',
    'セッションを捨てて新しい会話を始める': 'Discard the session and start a new conversation',
    '新規会話': 'New conversation',
    '生成される設定 YAML': 'Generated configuration YAML',
    '閉じる': 'Close',
    'プリセット名（拡張子なし）': 'Preset name (no extension)',
    '英数字・ハイフン・アンダースコア・ピリオドを使用できます。同名ファイルは上書きします。':
      'Letters, digits, hyphens, underscores and periods are allowed. An existing file with the same name is overwritten.',
    'キャンセル': 'Cancel',
    '保存': 'Save',

    /* ---- フォーム構築（index.html の JS 生成文言） ---- */
    '面表示': 'Solid',
    '面＋ワイヤーフレーム': 'Solid + wireframe',
    'ワイヤーフレームのみ': 'Wireframe only',
    '（なし）': '(none)',
    '（未指定）': '(unset)',
    '（未指定=デフォルト）': '(unset = default)',
    'absolute モードでは軌道から自動計算されるため無視されます': 'Ignored in absolute mode (computed from the orbit)',
    'mode=absolute のときだけ使われます': 'Used only when mode=absolute',
    'シミュレーション時間を指定中のため無視されます（dt = 時間 ÷ フレーム数）':
      'Ignored while Simulation time is set (dt = time ÷ frames)',
    'フォーム未対応のキー（そのまま適用）': 'Keys without form fields (applied as-is)',
    '読み込んだプリセットのうち、GUI フォームに入力欄が無いキーです。編集はできませんが、 レンダリング・ライブプレビュー・プリセット保存にはこのまま引き継がれます。':
      'Keys from the loaded preset that have no input field in the GUI form. They cannot be edited here but are passed through to rendering, live preview and preset saving.',
    'これらのキーを捨てて、フォームの内容だけで実行する': 'Discard these keys and run with the form contents only',
    'クリア': 'Clear',
    'フォーム未対応のキーを破棄しました': 'Discarded the keys without form fields',
    '{n} 件': '{n} keys',

    /* ---- シナリオ・プリセットカタログ ---- */
    '絶対軌道': 'Absolute orbit',
    '地球を周回する衛星の軌道・姿勢を確認': 'Check the orbit and attitude of a satellite around Earth',
    '相対軌道': 'Relative orbit',
    'ターゲットと観測側の接近・周回・近接撮像': 'Approach, fly-around and proximity imaging of a target',
    '地上観測': 'Ground observation',
    '地上の望遠鏡から見える像と明るさを確認': 'Check the image and brightness seen from a ground telescope',
    '標準軌道': 'Standard orbit',
    'CSV軌道・姿勢': 'CSV orbit & attitude',
    'CSVから再生': 'Replay from CSV',
    '複数物体': 'Multiple objects',
    '複数物体の軌道': 'Orbits of multiple objects',
    '地球背景を高品質化（追加適用）': 'High-quality Earth background (overlay)',
    '2機の軌道CSV・機載カメラ': 'Two orbit CSVs, onboard camera',
    '近接検査・30 m': 'Proximity inspection, 30 m',
    '汎用衛星': 'Generic satellite',
    '固定配置': 'Static placement',
    'タンブリング': 'Tumbling',
    '標準観測': 'Standard observation',
    '大気揺らぎあり': 'With atmospheric turbulence',
    '汎用GEO衛星': 'Generic GEO satellite',
    '定点観測': 'Stare observation',
    'サーベイ': 'Survey',
    'KUPT観測': 'KUPT observation',
    '単機タンブリング': 'Single-body tumbling',
    'カスタム': 'Custom',
    /* internal/presets の '# mrender-gui:' ヘッダと models/library の表示名（データ由来。辞書に無ければ原文表示） */
    'フライアラウンド': 'Fly-around',
    'フライアラウンド・地球背景': 'Fly-around with Earth background',
    '絶対軌道から相対運動を計算': 'Relative motion from absolute orbits',
    '近接検査': 'Proximity inspection',
    'Bull’s eye 観測再現': 'Bull’s eye observation replay',
    'あかつき': 'Akatsuki',
    'BULL HORN-L（薄膜）': 'BULL HORN-L (thin film)',
    'BULL HORN-R（薄膜）': 'BULL HORN-R (thin film)',
    'H-IIA上段（ADRAS-J）': 'H-IIA upper stage (ADRAS-J)',
    '編集中の設定を復元しました': 'Restored the settings being edited',
    'モデルを選んでマウスで形状を確認できます': 'Pick a model and inspect its shape with the mouse',
    'chief・deputyとカメラをそれぞれ選択できます': 'Select the chief, deputy and camera',
    'ターゲットを選ぶとプリセットを読み込みます': 'Selecting a target loads its preset',
    '背景なしでモデル形状を確認。ドラッグで視点回転・ホイールで拡大縮小':
      'Inspect the model shape without a background. Drag to rotate, wheel to zoom',
    'サンプルキューブ': 'Sample cube',
    '手動設定': 'Manual setup',
    'プリセットを選択': 'Select a preset',
    '3D表示を開始できません': 'Cannot start the 3D view',
    'モデル': 'Model',
    'フォームで編集': 'edited in form',
    'プリセットを選択してください': 'Select a preset',
    '{name} を読み込み中…': 'Loading {name}…',
    '{name} を追加適用しました': 'Applied {name} as an overlay',
    '{name} を読み込みました': 'Loaded {name}',
    '簡易衛星': 'Simple satellite',
    '{role} のモデルを変更しました（スケールは詳細設定で調整できます）':
      'Changed the {role} model (adjust the scale in the detailed settings)',

    /* ---- 実行マシン・ジョブ・ラン ---- */
    'local（このマシン）': 'local (this machine)',
    '実行マシン: {m}': 'Machine: {m}',
    'マシン切替エラー': 'Machine switch error',
    '起動中…': 'Starting…',
    'ジョブ {id} を開始（{dir}）': 'Started job {id} ({dir})',
    '停止': 'Stop',
    '最新フレーム': 'Latest frame',
    'ログ': 'Log',
    '{n} フレーム': '{n} frames',
    'CLI 相当: python mrender.py {verb} --config <この内容の.yaml>': 'CLI equivalent: python mrender.py {verb} --config <this .yaml>',
    '保存しました: {path}': 'Saved: {path}',
    'まだランがありません。': 'No runs yet.',
    '▶ 動画': '▶ Video',

    /* ---- ライブプレビュー（JS 生成） ---- */
    'ライブ更新に失敗しました (verb={verb}, HTTP {status}): {detail}': 'Live update failed (verb={verb}, HTTP {status}): {detail}',
    'ライブプレビュー API が使えません（サーバー側が未対応の可能性）。': 'The live preview API is unavailable (the server may not support it).',
    'ライブ更新に失敗しました': 'Live update failed',
    'フレーム取得に失敗しました (HTTP {status})': 'Failed to fetch the frame (HTTP {status})',
    'ライブプレビューに接続できません': 'Cannot connect to the live preview',
    '（ポーリングを停止しました）': ' (polling stopped)',
    'ライブプレビュー API (/api/live/status) がサーバーにありません。': 'The server has no live preview API (/api/live/status).',
    'ワーカーエラー': 'Worker error',
    'レンダ中': 'rendering',
    'エラー': 'error',
    '待機': 'idle',
    '操作中の簡易化描画（大気/雲/夜光/星空オフ・低バウンス）':
      'Simplified rendering during interaction (atmosphere/clouds/night lights/stars off, fewer bounces)',
    '簡易': 'fast',
    '{verb} のシーンを準備中…': 'Preparing the {verb} scene…',
    '（初回は Mitsuba の読み込みで 20 秒ほどかかることがあります）': ' (the first time can take ~20 s while Mitsuba loads)',
    '「太陽を回す」が ON のため無効です（フレームごとに自動回転します）':
      'Disabled because “Rotate the Sun” is ON (rotates automatically every frame)',
    '自動回転': 'Auto-rotate',
    'カメラ情報の取得待ちです（最初のライブ描画が出てから操作してください）':
      'Waiting for camera info (interact after the first live frame appears)',
    'モデルは静止しています。ドラッグで視点を回せます。': 'The model is static. Drag to rotate the view.',
    'カメラ = view_mode 既定（ドラッグすると上書きを開始します）': 'Camera = view_mode default (dragging starts an override)',
    '機載カメラは選択した機体・視点に固定されています。マウスで動かすには外部カメラを選択してください。':
      'The onboard camera is fixed to the selected body and view. Select the external camera to move it with the mouse.',
    'この verb のカメラは観測幾何から自動決定されます（ビューポート操作は無効）':
      'This verb’s camera is determined by the observation geometry (viewport controls disabled)',
    'ライブプレビューのワーカーを停止しました': 'Stopped the live preview worker',
    'ワーカー停止に失敗しました': 'Failed to stop the worker',

    /* ---- AI drawer（JS 生成） ---- */
    '⏹ 停止': '⏹ Stop',
    '実行を停止': 'Stop the run',
    '処理中…': 'Working…',
    'web 検索': 'web search',
    'todo 更新': 'todo update',
    '応答を待機中…': 'Waiting for a response…',
    '思考中…': 'Thinking…',
    '✓ 完了': '✓ Done',
    'AI がプリセットを更新': 'AI updated presets',
    '実行中です — 完了を待って再送信してください': 'A run is in progress — wait for it to finish before sending again',
    '{verb} の現在設定を添付': 'attached current {verb} settings',
    '接続中…': 'Connecting…',
    'このサーバでは AI アシスタントが無効です（localhost バインド時のみ利用できます）。':
      'The AI assistant is disabled on this server (available only when bound to localhost).',
    'このセッションは別タブ / 別クライアントで実行中です': 'This session is running in another tab / client',
    '別の実行が進行中です — 完了を待って再送信してください': 'Another run is in progress — wait for it to finish before sending again',
    'AI エラー': 'AI error',
    '接続エラー': 'Connection error',
    '開始直後です — 数秒待ってからもう一度': 'Just started — wait a few seconds and try again',
    '停止要求を送信…': 'Sending stop request…',
    '実行中です — 停止してから新規会話にしてください': 'A run is in progress — stop it before starting a new conversation',
    'AI CLI（claude / grok）が見つかりません。インストール後にサーバを再起動してください。':
      'No AI CLI (claude / grok) found. Install one and restart the server.',
    'AI アシスタントに接続できません（{err}）。GUI の他の機能はそのまま使えます。':
      'Cannot connect to the AI assistant ({err}). The rest of the GUI still works.',
    '初期化エラー': 'Initialization error',

    /* ---- gui_server.py が返すエラー ---- */
    'モデルが見つかりません': 'Model not found',
    '未対応のモデル形式です': 'Unsupported model format',
    'live worker が不正な応答を返しました': 'The live worker returned an invalid response',
    'プリセット名を指定してください': 'Specify a preset name',

    /* ---- object_viewer.js ---- */
    'モデルの3D表示。ドラッグで回転、ホイールで拡大縮小': '3D view of the model. Drag to rotate, wheel to zoom',
    '3D表示が中断されました。画面を再読み込みしてください。': 'The 3D view was interrupted. Reload the page.',
    'モデルファイルを読み込めません': 'Cannot load the model file',
    '外部の材質参照は読み込めません': 'External material references cannot be loaded',
    'モデルを読み込み中…': 'Loading model…',
    '頂点が潰れたモデルです。元のGLBを選択してください。': 'The model has degenerate vertices. Select the original GLB.',
    '{n} 三角形 · ブラウザ内で表示': '{n} triangles · shown in browser',
    'モデル表示エラー': 'Model display error',

    /* ---- フォームスキーマ（gui_server.py の label / help / title / desc） ---- */
    'render（軌道レンダ）': 'render (orbit rendering)',
    'ケプラー/数値伝播軌道 + 姿勢でフレーム連番を出力': 'Render a frame sequence from a Keplerian/numerically propagated orbit and attitude',
    'レンダリング': 'Rendering',
    'フレーム数': 'Frames',
    'サンプル数 (spp)': 'Samples (spp)',
    '高いほど綺麗・遅い。プレビュー 16–32、本番 128–512': 'Higher is cleaner but slower. Preview 16–32, production 128–512',
    '幅 [px]': 'Width [px]',
    '高さ [px]': 'Height [px]',
    'シミュレーション時間 [s]': 'Simulation time [s]',
    '未指定時は orbit_speed 由来の時間軸': 'If empty, the time axis follows orbit_speed',
    '開始時刻 [s]': 'Start time [s]',
    'エポック UTC (ISO 8601)': 'Epoch UTC (ISO 8601)',
    't=0 の実時刻。指定すると太陽=VSOP87・自転角=GMST（平均分点 of date）。空欄=簡易モデル（t=0 で太陽=+x・グリニッジ=+x）':
      'Real time at t=0. When set, Sun = VSOP87 and rotation angle = GMST (mean equinox of date). Empty = simple model (at t=0 Sun = +x, Greenwich = +x)',
    '開始フレーム': 'Start frame',
    'preview ではこのフレームだけ描画': 'preview renders only this frame',
    '完了後に mp4 作成': 'Make mp4 when done',
    '動画再生 fps': 'Video playback fps',
    'mp4 の再生速度のみ（物理・時間軸には無関係）': 'Playback speed of the mp4 only (no effect on physics or the time axis)',
    '主物体（軌道・モデル）': 'Primary object (orbit & model)',
    '名前': 'Name',
    '3Dモデル': '3D model',
    '未指定ならプロシージャル衛星': 'Procedural satellite if empty',
    '材質プリセット': 'Material preset',
    'モデル内蔵材質を使う': 'Use materials embedded in the model',
    'スケール（地球半径=1）': 'Scale (Earth radius = 1)',
    '姿勢モード': 'Attitude mode',
    '高度 [km]': 'Altitude [km]',
    '離心率 e': 'Eccentricity e',
    '軌道傾斜角 i [deg]': 'Inclination i [deg]',
    '昇交点赤経 Ω [deg]': 'RAAN Ω [deg]',
    '近地点引数 ω [deg]': 'Argument of periapsis ω [deg]',
    '平均近点角 M₀ [deg]': 'Mean anomaly M₀ [deg]',
    '軌道再生速度倍率': 'Orbit playback speed factor',
    '軌道 CSV (ephemeris)': 'Orbit CSV (ephemeris)',
    '指定すると軌道要素より優先': 'Takes precedence over orbital elements when set',
    '伝播器': 'Propagator',
    'J2 摂動': 'J2 perturbation',
    '大気抵抗': 'Atmospheric drag',
    '追加物体（objects: リスト）': 'Additional objects (objects: list)',
    'objects (YAML リスト)': 'objects (YAML list)',
    'YAML のリスト形式。name / csv_path / path / material / scale / altitude 等': 'YAML list. name / csv_path / path / material / scale / altitude, etc.',
    '視点モード': 'View mode',
    'satellite = 機載カメラ（view_camera_name → view_target_name）': 'satellite = onboard camera (view_camera_name → view_target_name)',
    'カメラ搭載物体名': 'Camera host object name',
    '注視物体名': 'Look-at object name',
    'カメラ位置（上書き）': 'Camera position (override)',
    '注視点（上書き）': 'Look-at point (override)',
    'up ベクトル': 'Up vector',
    'inertial 視点距離': 'inertial view distance',
    'chase 後方距離': 'chase distance behind',
    'chase 横距離': 'chase lateral distance',
    '地球': 'Earth',
    '昼テクスチャ': 'Day texture',
    '夜テクスチャ': 'Night texture',
    '夜の街明かり': 'Night city lights',
    '雲テクスチャ': 'Cloud texture',
    '雲レイヤ': 'Cloud layer',
    '雲の不透明度': 'Cloud opacity',
    '大気散乱': 'Atmospheric scattering',
    '大気密度': 'Atmosphere density',
    '地球自転': 'Earth rotation',
    '地球のみ描画': 'Render Earth only',
    'ライティング・トーン': 'Lighting & tone',
    '高度な光学（推奨）': 'Advanced optics (recommended)',
    '黒体放射太陽色・星空等の物理ベースライティング': 'Physically based lighting: blackbody Sun color, starfield, etc.',
    '太陽色温度 [K]': 'Sun color temperature [K]',
    '太陽黄経 [deg]': 'Sun ecliptic longitude [deg]',
    '黄道上の太陽位置（0=春分点方向 +x、90=夏至）。空欄=自動（epoch_utc 指定なら VSOP87）':
      'Sun position on the ecliptic (0 = vernal equinox +x, 90 = summer solstice). Empty = auto (VSOP87 when epoch_utc is set)',
    '太陽を回す': 'Rotate the Sun',
    '星空の明るさ': 'Starfield brightness',
    'HDRI 環境マップ': 'HDRI environment map',
    '軌道の軌跡線を描画': 'Draw orbit trail',
    '露出 [stop]': 'Exposure [stop]',
    'ガンマ': 'Gamma',
    'preview（1フレーム）': 'preview (single frame)',
    '開始フレームだけ即レンダ（材質・構図調整用）': 'Render only the start frame immediately (for tuning materials and composition)',
    'onboard（機載カメラ）': 'onboard (onboard camera)',
    '別物体を機載カメラで注視（view_mode=satellite 既定）': 'Look at another object from an onboard camera (view_mode=satellite by default)',
    'lightcurve（光度曲線）': 'lightcurve (light curve)',
    '地上観測者から見た明るさの時系列を CSV 出力': 'Write a CSV time series of brightness as seen by a ground observer',
    'ライトカーブ / 観測者': 'Light curve / observer',
    '観測 FOV [deg]': 'Observation FOV [deg]',
    '空欄=物体の見かけサイズから自動（クリップ防止）': 'Empty = auto from the object’s apparent size (avoids clipping)',
    '観測サンプル数': 'Observation samples',
    '観測者緯度 [deg]': 'Observer latitude [deg]',
    '観測者経度 [deg]': 'Observer longitude [deg]',
    '観測者高度 [km]': 'Observer altitude [km]',
    'relative（2機の相対配置）': 'relative (two-spacecraft relative placement)',
    '相対状態または2機の絶対軌道から近接撮像（OOS）': 'Proximity imaging (OOS) from a relative state or two absolute orbits',
    '時間刻み fps': 'Time step fps',
    'dt = 1/fps。時間 [s] 欄を指定中は無視される（dt = duration/frames）': 'dt = 1/fps. Ignored while Simulation time [s] is set (dt = duration/frames)',
    '指定時 dt = duration/frames': 'When set, dt = duration/frames',
    '並列プロセス数': 'Parallel processes',
    'フレーム範囲を N 分割して並列レンダ。GPU 実行 × 多フレームでほぼ線形にスケール（少フレームではプロセス起動が勝って逆効果）':
      'Split the frame range into N chunks rendered in parallel. Scales almost linearly for GPU × many frames (counterproductive for few frames, where process startup dominates)',
    'モード・相対状態': 'Mode & relative state',
    'モード': 'Mode',
    'static=固定 / csv=時系列再生 / tumble=deputy タンブリング / absolute=絶対軌道 2 本から相対状態+環境を導出':
      'static = fixed / csv = replay a time series / tumble = deputy tumbling / absolute = derive the relative state and environment from two absolute orbits',
    'deputy 相対位置 [km]': 'deputy relative position [km]',
    '相対位置の座標系': 'Relative position frame',
    'hill=Hill/RTN（chief原点）、chief=旧形式のchief機体系': 'hill = Hill/RTN (chief origin), chief = legacy chief body frame',
    '相対姿勢 [qx qy qz qw]': 'Relative attitude [qx qy qz qw]',
    'absolute+tumble では t=0 の RTN 相対初期姿勢': 'For absolute+tumble: initial RTN-relative attitude at t=0',
    '相対状態 CSV': 'Relative state CSV',
    'mode=csv 時必須（time_s,x,y,z,qx,qy,qz,qw）': 'Required for mode=csv (time_s,x,y,z,qx,qy,qz,qw)',
    '絶対軌道（mode=absolute）': 'Absolute orbits (mode=absolute)',
    'エポック UTC': 'Epoch UTC',
    'ISO 8601。太陽位置 (VSOP87)・GMST (地球姿勢) の t=0 基準': 'ISO 8601. Reference t=0 for the Sun position (VSOP87) and GMST (Earth attitude)',
    'chief 軌道要素': 'chief orbital elements',
    'deputy 軌道要素': 'deputy orbital elements',
    '[a_km, e, i°, Ω°, ω°, M0°]（CSV 未指定なら必須）': '[a_km, e, i°, Ω°, ω°, M0°] (required unless a CSV is given)',
    'chief 軌道 CSV': 'chief orbit CSV',
    'CsvEphemeris スキーマ（rad）。指定時は軌道要素より優先': 'CsvEphemeris schema (rad). Takes precedence over orbital elements when set',
    'deputy 軌道 CSV': 'deputy orbit CSV',
    'deputy 姿勢': 'deputy attitude',
    'tumble=ECI 系トルクフリー伝播 / lvlh=deputy RTN+rel_quat 固定 / csv=軌道 CSV の q1..q4。太陽方向・地心方向・高度・地球回転は軌道から自動計算（宇宙環境の該当欄は無視）':
      'tumble = torque-free propagation in ECI / lvlh = fixed to deputy RTN + rel_quat / csv = q1..q4 from the orbit CSV. Sun direction, nadir direction, altitude and Earth rotation are computed from the orbit (the corresponding Space environment fields are ignored)',
    'タンブル動力学（mode=tumble）': 'Tumble dynamics (mode=tumble)',
    'chief（サービサ側）': 'chief (servicer side)',
    'chief を描画しない（カメラ=機載視点）': 'Do not render chief (camera = onboard view)',
    'chief モデル': 'chief model',
    'chief スケール': 'chief scale',
    'chief 位置 [km]': 'chief position [km]',
    'Hill座標系の原点 [0, 0, 0] に固定': 'Fixed at the Hill frame origin [0, 0, 0]',
    'パーツ別 BSDF (YAML)': 'Per-part BSDF (YAML)',
    'デフォルト BSDF (YAML)': 'Default BSDF (YAML)',
    'chief 姿勢 [qx qy qz qw]': 'chief attitude [qx qy qz qw]',
    'deputy（ターゲット側）': 'deputy (target side)',
    'deputy モデル': 'deputy model',
    'deputy スケール': 'deputy scale',
    'モデル単位が m なら 0.001（シーンは km 単位）': '0.001 if the model is in metres (scene units are km)',
    "OBJ の 'o' パーツ名 → Mitsuba BSDF 辞書": "OBJ 'o' part name → Mitsuba BSDF dict",
    '太陽電池パネル（PV 入射照度・発電量）': 'Solar panels (PV irradiance & power)',
    '仮想パネル 幅×高さ [m]': 'Virtual panel width × height [m]',
    '指定すると 1 枚追加し、フレームごとに直達/地球照/その他 [W/m²] と発電量 [W] を pv_irradiance.csv に出力':
      'Adds one panel and writes direct/earthshine/other irradiance [W/m²] and power [W] per frame to pv_irradiance.csv',
    'パネル中心 [m・機体系]': 'Panel center [m, body frame]',
    '受光面法線 [機体系]': 'Receiving-face normal [body frame]',
    '片面受光': 'Single-sided',
    '面内の上方向 [機体系]': 'In-plane up direction [body frame]',
    '取付機体': 'Host spacecraft',
    '受光面にする OBJ パーツ (YAML リスト)': 'OBJ parts used as receiving faces (YAML list)',
    '閉じたメッシュは表裏全フェイスで平均される': 'Closed meshes are averaged over all faces, front and back',
    '複数パネル定義 (YAML)': 'Multiple panel definitions (YAML)',
    '発電効率 η': 'Conversion efficiency η',
    '太陽定数 [W/m²]': 'Solar constant [W/m²]',
    '間接光パス数/フレーム': 'Indirect-light paths per frame',
    '直達 遮蔽サンプル点数': 'Direct-light shadowing sample points',
    '地球アルベドマップを実データで自動生成': 'Auto-generate Earth albedo map from real data',
    'MODIS 雲分率・雲光学的厚さ（NASA GIBS）+ 地表アルベドの 2 層モデル。日付ごとに runs/_gibs_cache/albedo/ にキャッシュ（要ネットワーク）':
      'Two-layer model: MODIS cloud fraction & optical thickness (NASA GIBS) + surface albedo. Cached per date in runs/_gibs_cache/albedo/ (needs network)',
    '既製アルベドマップ PNG': 'Prebuilt albedo map PNG',
    'earth_albedo_map.py の出力。指定時は一様アルベド/テクスチャより優先': 'Output of earth_albedo_map.py. Takes precedence over uniform albedo/texture when set',
    'アルベドマップの日付': 'Albedo map date',
    'YYYY-MM-DD。空欄=フレームの UTC 日付': 'YYYY-MM-DD. Empty = the frame’s UTC date',
    'マップ解像度 (GIBS level)': 'Map resolution (GIBS level)',
    '環境光も計測に含める': 'Include ambient light in the measurement',
    '既定は除外（既定の淡い環境光は可視化用で物理量ではない）': 'Excluded by default (the faint default ambient light is for visualization, not a physical quantity)',
    '地球を一様アルベド球にする': 'Use a uniform-albedo Earth sphere',
    '空欄=テクスチャ地球。0.3 で雲込み平均アルベドのランバート球（解析検証用）':
      'Empty = textured Earth. 0.3 gives a Lambertian sphere with the cloud-inclusive mean albedo (for analytic validation)',
    'カメラモード': 'Camera mode',
    '取付位置 [km・機体系]': 'Mount position [km, body frame]',
    '固定視線方向・機体系': 'Fixed line of sight, body frame',
    'fixed のときのみ使用': 'Used only for fixed modes',
    '上方向・機体系': 'Up direction, body frame',
    'カメラ位置 [km]': 'Camera position [km]',
    '注視点 [km]': 'Look-at point [km]',
    '宇宙環境（フォトリアル）': 'Space environment (photoreal)',
    '地球を背景に描画': 'Render Earth in the background',
    '地心方向（シーン座標）': 'Nadir direction (scene coordinates)',
    '地球テクスチャ': 'Earth texture',
    '地球テクスチャ回転 [deg]': 'Earth texture rotation [deg]',
    'GIBS 実写画像（雲込み・要ネット）': 'GIBS real imagery (with clouds, needs network)',
    'NASA GIBS から可視域だけオンデマンド取得（MODIS 250 m/px、実写日次。取得失敗時はローカルテクスチャへ自動フォールバック）':
      'Fetch only the visible area on demand from NASA GIBS (MODIS 250 m/px, daily imagery. Falls back to the local texture on failure)',
    'GIBS 撮像日': 'GIBS imaging date',
    'YYYY-MM-DD（未指定は昨日 UTC。Terra は 2000-02-24〜今日）': 'YYYY-MM-DD (default: yesterday UTC. Terra covers 2000-02-24 to today)',
    'GIBS レイヤ': 'GIBS layer',
    '例: VIIRS_SNPP_CorrectedReflectance_TrueColor': 'e.g. VIIRS_SNPP_CorrectedReflectance_TrueColor',
    '太陽光の進行方向': 'Sunlight direction of travel',
    '太陽強度': 'Sun intensity',
    '例 5778。未指定は従来色': 'e.g. 5778. Empty = legacy color',
    '星空 envmap (EXR)': 'Starfield envmap (EXR)',
    '環境光/星空の明るさ': 'Ambient/starfield brightness',
    '最大バウンス数': 'Max bounces',
    '可視化補助': 'Visualization aids',
    '慣性軸を描画': 'Draw inertial axes',
    '機体軸を描画': 'Draw body axes',
    'ブラウザ内でモデルを直接表示（レンダリング待ちなし）': 'Show the model directly in the browser (no render wait)',
    '表示': 'Display',
    '描画方法': 'Draw style',
    'モデルの材質を使用': 'Use model materials',
    'rotation（単機タンブリング）': 'rotation (single-body tumbling)',
    '軌道なし。オイラー回転運動方程式で単機を回す': 'No orbit. Spin a single body with Euler’s rotation equations',
    'スケール': 'Scale',
    '回転動力学': 'Rotational dynamics',
    'カメラ位置': 'Camera position',
    '注視点': 'Look-at point',
    'groundobs（地上望遠鏡観測）': 'groundobs (ground telescope observation)',
    '地上局からの見かけ等級ライトカーブ + 望遠鏡センサ像（点像〜分解像）':
      'Apparent-magnitude light curve from a ground station + telescope sensor image (point source to resolved)',
    '時間軸（UTC エポック）': 'Time axis (UTC epoch)',
    't=0 の UTC 時刻。GMST・太陽位置の基準': 'UTC time at t=0. Reference for GMST and the Sun position',
    '総観測時間 [s]': 'Total observation time [s]',
    '開始オフセット [s]': 'Start offset [s]',
    '観測地（地上局）': 'Observation site (ground station)',
    '緯度 [deg]（北+）': 'Latitude [deg] (north +)',
    '経度 [deg]（東+）': 'Longitude [deg] (east +)',
    '標高 [m]': 'Elevation [m]',
    '最低仰角 [deg]': 'Minimum elevation angle [deg]',
    '軌道': 'Orbit',
    'TLE ファイル': 'TLE file',
    'SGP4 伝播（TEME≈ECI 近似）。CSV・ケプラー要素より優先。epoch 未指定時は TLE エポックを t=0 に採用':
      'SGP4 propagation (TEME≈ECI). Takes precedence over CSV and Keplerian elements. Without an epoch, the TLE epoch is used as t=0',
    'TLE 衛星名（部分一致）': 'TLE satellite name (substring match)',
    '指定時はケプラー要素より優先': 'Takes precedence over Keplerian elements when set',
    '円軌道高度 [km]': 'Circular orbit altitude [km]',
    '軌道長半径 a [km]': 'Semi-major axis a [km]',
    't=0 で天頂パスに整列': 'Align a zenith pass at t=0',
    'Ω と M₀ を自動調整（デモ・可視パス作成用）': 'Auto-adjust Ω and M₀ (for demos / creating a visible pass)',
    '姿勢': 'Attitude',
    '未指定なら拡散球ターゲット': 'Diffuse sphere target if empty',
    'モデル単位→km 倍率': 'Model units → km factor',
    'm 単位モデルなら 0.001': '0.001 for a model in metres',
    '球半径 [m]': 'Sphere radius [m]',
    '球アルベド': 'Sphere albedo',
    '観測モード・恒星背景': 'Observation mode & star background',
    '追尾モード': 'Tracking mode',
    'target=物体追尾（恒星が流れる）/ sidereal=恒星時追尾（物体がストリーク）': 'target = track the object (stars trail) / sidereal = sidereal tracking (the object streaks)',
    '恒星背景を描画': 'Draw star background',
    '恒星カタログ (npz)': 'Star catalog (npz)',
    '恒星の限界等級': 'Limiting magnitude of stars',
    '大気屈折を適用': 'Apply atmospheric refraction',
    '望遠鏡・大気': 'Telescope & atmosphere',
    'ピクセルスケール ["/px]': 'Pixel scale ["/px]',
    'センサ一辺 [px]': 'Sensor side [px]',
    '内部スーパーサンプル': 'Internal supersampling',
    'シーイング FWHM ["]': 'Seeing FWHM ["]',
    '減光係数 k [mag/airmass]': 'Extinction coefficient k [mag/airmass]',
    'センサノイズ (CCD)': 'Sensor noise (CCD)',
    'CCD ノイズモデルを適用': 'Apply CCD noise model',
    '口径 [m]': 'Aperture [m]',
    'スループット': 'Throughput',
    '露光時間 [s]': 'Exposure time [s]',
    '読み出しノイズ [e⁻]': 'Read noise [e⁻]',
    'ゲイン [e⁻/ADU]': 'Gain [e⁻/ADU]',
    'フルウェル [e⁻]': 'Full well [e⁻]',
    '夜空輝度 [mag/arcsec²]': 'Sky brightness [mag/arcsec²]',
    'ノイズシード': 'Noise seed',
    '地球アルベド（一様ランバート球）': 'Earth albedo (uniform Lambertian sphere)',
    '雲込み全球平均 0.29〜0.30（CERES）。earth_texture 指定時は無視': 'Cloud-inclusive global mean 0.29–0.30 (CERES). Ignored when earth_texture is set',
    '指定時は可視域クロップ + GMST 姿勢のテクスチャ地球（BMNG は雲なしで暗く、地球照は下限値）':
      'When set, a textured Earth with visible-area crop + GMST attitude (BMNG is cloud-free and dark, so earthshine is a lower bound)',
    '観測像にも地球照を含める': 'Include earthshine in the observation image',
    '物体への照り返しを測光・像に乗せる（既定 OFF = 太陽のみ）': 'Add Earth’s reflected light on the object to photometry and the image (default OFF = Sun only)',
    '出力': 'Output',
    'ストレッチ': 'Stretch',
    'ストレッチ percentile': 'Stretch percentile',
    'リニア画像 (.npy) も保存': 'Also save the linear image (.npy)',
  };

  const LS_KEY = 'mrender.lang';
  const norm = (s) => s.replace(/\s+/g, ' ').trim();

  const inBrowser = !!(global.document && global.document.documentElement);

  function detect() {
    let pref = null;
    if (inBrowser) {
      try { pref = new URLSearchParams(global.location.search).get('lang'); } catch (_) { /* no location */ }
      if (!pref) { try { pref = global.localStorage.getItem(LS_KEY); } catch (_) { /* private mode 等 */ } }
    }
    if (pref !== 'ja' && pref !== 'en') pref = 'auto';
    const nav = (global.navigator && (global.navigator.language || (global.navigator.languages || [])[0])) || 'ja';
    const sys = String(nav).toLowerCase().startsWith('ja') ? 'ja' : 'en';
    return { pref, lang: pref === 'auto' ? sys : pref };
  }

  const state = detect();

  /* 原文 → 表示文言。vars で {name} を置換。文字列以外はそのまま返す。 */
  function t(s, vars) {
    if (typeof s !== 'string') return s;
    let out = s;
    if (state.lang !== 'ja') {
      const hit = DICT[s] != null ? DICT[s] : DICT[norm(s)];
      if (hit != null) out = hit;
    }
    if (vars) out = out.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
    return out;
  }

  const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
  const SKIP = new Set(['SCRIPT', 'STYLE', 'TEXTAREA', 'PRE', 'CODE']);

  /* root 以下の静的テキストと属性を置換する（日本語モードでは何もしない）。 */
  function translateDom(root) {
    if (state.lang === 'ja' || !root || !global.document) return;
    const doc = global.document;
    const walker = doc.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (n.nodeType === 1) {
          // translate="no"（言語名「日本語」など）は属性・子孫ごと対象外。
          // script/style/textarea/pre 等は要素自体（placeholder/title 属性）は見るが、中身のテキストは触らない。
          return n.getAttribute('translate') === 'no' ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
        }
        return (n.parentNode && SKIP.has(n.parentNode.tagName)) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      },
    });
    const texts = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.nodeType === 1) {
        for (const a of ATTRS) {
          if (!n.hasAttribute(a)) continue;
          const v = n.getAttribute(a);
          const hit = DICT[v] != null ? DICT[v] : DICT[norm(v)];
          if (hit != null) n.setAttribute(a, hit);
        }
      } else {
        texts.push(n);
      }
    }
    for (const n of texts) {
      const raw = n.nodeValue;
      const key = norm(raw);
      if (!key) continue;
      const hit = DICT[key];
      if (hit == null) continue;
      // 前後の空白は保持（<label>テーマ\n<select> のようなレイアウト依存の空白）
      const lead = raw.match(/^\s*/)[0], trail = raw.match(/\s*$/)[0];
      n.nodeValue = lead + hit + trail;
    }
  }

  const SCHEMA_KEYS = new Set(['label', 'desc', 'title', 'help', 'placeholder']);

  /* /api/state の verbs スキーマ（label/desc/title/help）と preset_meta
   * （[scenario, target, label, verb]）を表示言語に置換した新オブジェクトを返す。 */
  function translateSchema(obj) {
    if (state.lang === 'ja') return obj;
    const walk = (o, key) => {
      if (Array.isArray(o)) return o.map((v) => walk(v, null));
      if (o && typeof o === 'object') {
        const out = {};
        for (const [k, v] of Object.entries(o)) out[k] = walk(v, k);
        return out;
      }
      if (typeof o === 'string' && key && SCHEMA_KEYS.has(key)) return t(o);
      return o;
    };
    return walk(obj, null);
  }

  function setPreference(pref) {
    if (!inBrowser) return;
    try { global.localStorage.setItem(LS_KEY, pref); } catch (_) { /* 保存できなくても reload 後は auto */ }
  }

  const I18N = { lang: state.lang, pref: state.pref, t, translateDom, translateSchema, setPreference, dict: DICT };

  if (inBrowser) global.document.documentElement.lang = state.lang;
  global.I18N = I18N;
  if (typeof module !== 'undefined' && module.exports) module.exports = I18N;
})(typeof window !== 'undefined' ? window : globalThis);

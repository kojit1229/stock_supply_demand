# CLAUDE.md — stock_supply_demand(需給ナビ)

個別銘柄の需給推移(JSDA週次貸借残高・JPX空売り残高報告・株価)を可視化する検索型PWA。
**研究・分析ツールであり投資助言ではない。銘柄推奨機能を作らない。**

設計の正典: `C:\Users\kojit\Documents\ClaudeCode\workbench\out\2026-07-22-supply-demand-app-design\design.md`
(データソース実測監査: 同フォルダ `audit-notes.md`)

## 構成

- `index.html` / `sw.js` / `manifest.json` … PWA本体(単一ファイル型。外部ライブラリ禁止・Canvasチャート自前描画)
- `collector/` … Python収集・ビルド(依存は標準ライブラリ+openpyxl+xlrdのみ。AI呼び出しなし=完全決定論)
- `config/price_list.json` … 株価収集の対象銘柄(手動編集)
- `.github/workflows/` … weekly.yml(JSDA)/ daily.yml(JPX空売り)/ ci.yml
- **mainブランチに生成データを置かない**。生成物(data/*)はActionsがgh-pagesブランチへ単一コミットでforce更新(履歴を持たせない。設計書§3)

## 絶対ルール

1. パース失敗・検証失敗は握りつぶさない。**不正データはファイルを出さず(deployせず)exit 1**(フェイルラウド)
2. 銘柄コードはJSDAの5桁統一コードの**末尾が'0'の場合のみ4桁化**(285A0→285A)。末尾'0'以外の優先株・社債型種類株式(例: 25935)は5桁のまま独立銘柄として扱う。コード列int/str混在は必ずstr化で吸収
3. 数値は生値保存(株・百万円)。前週比の`'-'`はnull。末尾「合計」行は除外し検算に使う
4. 外部サーバへのリクエストは間隔を空ける(JSDAは連続アクセスでブロックされる実績あり)。User-Agent明示
5. テストはネットワークを叩かない(フィクスチャは実ファイル由来の縮小xlsx)
6. iOS Safari制約(相場帳と同一): `new Date(文字列)`禁止・モジュール変数はrender()より上で宣言・入力欄を含む再描画は差分DOMパッチ
7. Service Worker変更時はキャッシュ名v+1

## コマンド

```bash
PYTHONUTF8=1 python -m unittest discover -s tests   # パーサ・ビルドのテスト
```

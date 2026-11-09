import React from "react";

import {
  DataComponent, DataTable, ReportSection, RichNarrative, useDataApp,
} from "../../data-app-public.jsx";

const percent = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
const dateTime = (value) => value
  ? new Intl.DateTimeFormat("ko-KR", { timeZone: "America/New_York", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value))
  : "—";

const resultColumns = [
  { field: "game", label: "경기" },
  { field: "probability", label: "추정 승률", renderCell: (value) => percent(value) },
  { field: "favored", label: "모델 우세팀" },
  { field: "score", label: "최종 점수 (원정–홈)" },
  { field: "outcome", label: "결과", presentation: "status" },
];

const diagnosticColumns = [
  { field: "cohort", label: "코호트" },
  { field: "n", label: "표본 수" },
  { field: "wins", label: "승" },
  { field: "losses", label: "패" },
  { field: "hitRate", label: "적중률", renderCell: (value) => percent(value) },
  { field: "meanProbability", label: "평균 추정 승률", renderCell: (value) => percent(value) },
  { field: "brier", label: "Brier" },
  { field: "logLoss", label: "Log loss" },
];

const followupColumns = [
  { field: "priority", label: "우선순위" },
  { field: "finding", label: "확인할 내용" },
  { field: "action", label: "조치" },
  { field: "acceptance", label: "완료 기준" },
];

function EvidenceTable({ id, title, queryId, rows, sourceRows, columns }) {
  return <DataComponent id={id} title={title} queryId={queryId} kind="table"
    displayRows={rows} sourceRows={sourceRows}>
    <DataTable rows={rows} columns={columns} searchable={false} compactNumbers={false} label={title} />
  </DataComponent>;
}

export function ReportContent() {
  const { queries, reviewedRows, visible, appTitle, setAppTitle, canEdit, mode } = useDataApp();
  const results = reviewedRows("game_results");
  const predicted = results.filter((row) => Number.isFinite(row.probability) && row.outcome);
  const losses = predicted.filter((row) => row.outcome === "LOSS");
  const wins = predicted.filter((row) => row.outcome === "WIN");
  const mistakeNotes = reviewedRows("mistake_notes");
  const diagnosticSummary = reviewedRows("diagnostic_summary");
  const followupChecks = reviewedRows("followup_checks");
  const modelValidation = reviewedRows("model_validation");
  const hasMistakeNotes = Boolean(queries?.mistake_notes);
  const mistakeQueryId = hasMistakeNotes ? "mistake_notes" : "game_results";
  const mistakeSourceRows = hasMistakeNotes ? mistakeNotes : losses;
  const mistakeDisplayRows = hasMistakeNotes ? mistakeNotes : losses.map((row) => ({
    gamePk: row.gamePk,
    game: row.game,
    observed: `${row.score} · ${row.outcome}`,
    lesson: "오답별 교훈 데이터 연결 대기",
    nextCheck: "진단 요약 추가 후 확인",
  }));
  const cohorts = [...new Set(predicted.map((row) => row.cohort))];
  const hitRate = predicted.length ? wins.length / predicted.length : null;

  const resultDisplayRows = results.map((row) => ({
    game: row.game,
    probability: row.probability,
    favored: row.favored ?? "미산출",
    score: row.score,
    outcome: row.outcome === "WIN" ? "적중" : row.outcome === "LOSS" ? "실패" : "평가 제외",
    forecastAt: dateTime(row.forecastAt),
  }));

  return <article className="report-content error-notebook-report" aria-label="9월 2일 MLB 오답노트">
    <header className="report-hero">
      <h1 data-data-app-title contentEditable={canEdit && mode === "edit"} suppressContentEditableWarning
        aria-label={canEdit && mode === "edit" ? "리포트 제목 편집" : undefined}
        onBlur={canEdit && mode === "edit" ? (event) => setAppTitle(event.currentTarget.textContent.trim() || appTitle) : undefined}
        onKeyDown={canEdit && mode === "edit" ? (event) => {
          if (event.key === "Enter") { event.preventDefault(); event.currentTarget.blur(); }
        } : undefined}>{appTitle}</h1>
      <RichNarrative id="report:description" className="report-deck" label="오답노트 소개 편집"
        value="9월 2일 고정 예측을 공식 최종 결과와 대조하고, 틀린 경기를 다음 검증 항목으로 연결합니다." />
    </header>

    {visible("summary") && <ReportSection id="summary" title="이번 회고의 결론" queryId="game_results"
      sourceRows={results} showHeading={false} className="report-summary">
      <RichNarrative id="summary:body" className="report-summary-lead" label="요약 편집"
        value={`## 이번 회고의 결론\n\n예측이 있는 ${predicted.length}경기에서 ${wins.length}/${predicted.length} 적중(${percent(hitRate)}), ${losses.length}경기 실패였습니다. 두 코호트(${cohorts.join(" / ") || "—"})를 분리해 읽어야 하며, 이 수치는 확률 검증이나 수익률이 아닙니다. 모델 우세팀과 추정 승률은 사전 고정 출력이고 실제 베팅 성과를 뜻하지 않습니다.`} />
    </ReportSection>}

    {visible("results-section") && <ReportSection id="results-section" title="전체 결과 15경기" queryId="game_results"
      sourceRows={results} showHeading={false} className="report-section">
      <RichNarrative id="results:intro" label="결과표 설명 편집"
        value="## 전체 결과 15경기\n\n예측이 있었던 13경기만 적중/실패를 평가했고, 미산출 2경기는 결과표에 보존했습니다. 표의 값은 읽기 전용 검증 행입니다." />
      <EvidenceTable id="results" title="9월 2일 경기 결과" queryId="game_results"
        rows={resultDisplayRows} sourceRows={results} columns={resultColumns} />
    </ReportSection>}

    {visible("mistakes-section") && <ReportSection id="mistakes-section" title="오답 9개와 다음 확인" queryId={mistakeQueryId}
      sourceRows={mistakeSourceRows} showHeading={false} className="report-section">
      <RichNarrative id="mistakes:intro" label="오답 영역 설명 편집"
        value={`## 오답 9개와 다음 확인\n\n${losses.length}개의 LOSS를 경기 단위로 기록합니다. 관찰된 사실과 교훈, 다음 확인 항목을 분리해 원인 단정 없이 후속 검증으로 넘깁니다.`} />
      <div className="mistake-notes" aria-label="오답별 메모">
        {mistakeDisplayRows.map((row) => <ReportSection key={row.gamePk} id={`mistake-${row.gamePk}`}
          title={row.game} queryId={mistakeQueryId} sourceRows={mistakeSourceRows.filter((source) => source.gamePk === row.gamePk)}
          showHeading={false} className="mistake-note">
          <RichNarrative id={`mistake-${row.gamePk}:body`} label={`${row.game} 오답 메모 편집`}
            value={`### ${row.game}\n\n**관찰:** ${row.observed || "—"}\n\n**교훈:** ${row.lesson || "—"}\n\n**다음 확인:** ${row.nextCheck || "—"}`} />
        </ReportSection>)}
      </div>
    </ReportSection>}

    {queries?.diagnostic_summary && visible("diagnostics-section") && <ReportSection id="diagnostics-section" title="코호트별 진단" queryId="diagnostic_summary"
      sourceRows={diagnosticSummary} showHeading={false} className="report-section">
      <RichNarrative id="diagnostics:intro" label="진단 설명 편집"
        value="## 코호트별 진단\n\n전체 합산은 설명용이며, 기존 기록 6개와 사전 등록 7개는 별도 평가합니다. 각 표본, 적중률, 확률 오차 지표를 함께 확인합니다." />
      <EvidenceTable id="diagnostics" title="코호트별 성능 진단" queryId="diagnostic_summary"
        rows={diagnosticSummary} sourceRows={diagnosticSummary} columns={diagnosticColumns} />
    </ReportSection>}

    {queries?.model_validation && visible("model-validation-section") && <ReportSection id="model-validation-section" title="확률 검증의 한계" queryId="model_validation"
      sourceRows={modelValidation} showHeading={false} className="report-section">
      <RichNarrative id="model-validation:intro" label="모델 검증 설명 편집"
        value="## 확률 검증의 한계\n\n확률 보정은 되어 있으나 같은 표본을 사용(`IN_SAMPLE_NOT_OOF`)해 독립 검증이 부족합니다. 따라서 이번 수치는 독립적인 일반화 성능으로 읽지 않습니다." />
      <EvidenceTable id="model-validation" title="모델 검증 메모" queryId="model_validation"
        rows={modelValidation.map(({ finding, implication }) => ({ finding, implication }))}
        sourceRows={modelValidation} columns={[{ field: "finding", label: "확인된 사실" }, { field: "implication", label: "해석" }]} />
    </ReportSection>}

    {queries?.followup_checks && visible("next-checks-section") && <ReportSection id="next-checks-section" title="미확인 원인과 개선 검증" queryId="followup_checks"
      sourceRows={followupChecks} showHeading={false} className="report-section">
      <RichNarrative id="next-checks:intro" label="후속 검증 설명 편집"
        value="## 미확인 원인과 개선 검증\n\n현재 오답만으로 원인을 확정하지 않습니다. 아래 우선순위별 확인과 완료 기준을 통과한 뒤 모델 개선 여부를 판단합니다." />
      <EvidenceTable id="next-checks" title="후속 검증 체크리스트" queryId="followup_checks"
        rows={followupChecks} sourceRows={followupChecks} columns={followupColumns} />
    </ReportSection>}

    {visible("scope-section") && <ReportSection id="scope-section" title="해석 범위" queryId="game_results"
      sourceRows={results} showHeading={false} className="report-section report-scope">
      <RichNarrative id="scope:body" className="report-disclosure" label="해석 범위 편집"
        value="## 해석 범위\n\n이 회고는 저장된 사전 예측과 MLB 공식 최종 결과를 대조한 기록입니다. 미산출 경기는 성능 평가에서 제외했고, 원본 예측·배당을 변경하지 않았습니다. 실제 베팅 금액, 배당 수익, 수수료가 없으므로 실제 베팅 성과나 투자 판단으로 해석하지 않습니다." />
    </ReportSection>}
  </article>;
}

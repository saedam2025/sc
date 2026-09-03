import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "file:///C:/Users/lunch/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs";

const outputDir = path.resolve("outputs/excellent-instructor-roster-20260827");
await fs.mkdir(outputDir, { recursive: true });

const workbook = Workbook.create();
const roster = workbook.worksheets.add("등록양식");
roster.showGridLines = false;
roster.getRange("A1:E1").values = [["성명", "주민번호", "학교명", "직책", "강의과목"]];
roster.getRange("A1:E1").format = {
  fill: "#1E3A5F",
  font: { bold: true, color: "#FFFFFF", size: 11 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
roster.getRange("A1:E1").format.rowHeight = 28;
roster.getRange("A2:E21").format = {
  fill: "#FFFFFF",
  font: { color: "#334155", size: 10 },
  verticalAlignment: "center",
  borders: { preset: "all", style: "thin", color: "#CBD5E1" },
};
roster.getRange("A2:E21").format.rowHeight = 22;
roster.getRange("A2:A21").format.columnWidth = 14;
roster.getRange("B2:B21").format.columnWidth = 20;
roster.getRange("C2:C21").format.columnWidth = 25;
roster.getRange("D2:D21").format.columnWidth = 23;
roster.getRange("E2:E21").format.columnWidth = 25;
roster.getRange("B2:B21").format.numberFormat = "@";
roster.freezePanes.freezeRows(1);

const guide = workbook.worksheets.add("작성안내");
guide.showGridLines = false;
guide.mergeCells("A1:E1");
guide.getRange("A1").values = [["우수강사인증자 등록 양식 작성안내"]];
guide.getRange("A1:E1").format = {
  fill: "#1E3A5F",
  font: { bold: true, color: "#FFFFFF", size: 15 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
guide.getRange("A1:E1").format.rowHeight = 36;
guide.getRange("A3:B8").values = [
  ["항목", "작성 기준"],
  ["성명", "신청자 실명"],
  ["주민번호", "000000-0000000 형식 또는 숫자 13자리"],
  ["학교명", "근무 학교의 정식 명칭"],
  ["직책", "예: 방과후 강사, 늘봄학교 선택형 강사"],
  ["강의과목", "예: 바둑, 로봇코딩"],
];
guide.getRange("A3:B3").format = {
  fill: "#DBEAFE",
  font: { bold: true, color: "#1E3A5F" },
  horizontalAlignment: "center",
};
guide.getRange("A4:A8").format = { fill: "#F8FAFC", font: { bold: true, color: "#334155" } };
guide.getRange("A3:B8").format.verticalAlignment = "center";
guide.getRange("A3:B8").format.rowHeight = 26;
guide.getRange("A3:A8").format.columnWidth = 18;
guide.getRange("B3:B8").format.columnWidth = 52;
guide.mergeCells("A10:E10");
guide.getRange("A10").values = [["같은 강사가 여러 학교 또는 여러 과목에 해당하면 등록양식에서 행을 나누어 모두 입력해 주세요."]];
guide.getRange("A10:E10").format = {
  fill: "#FFF7ED",
  font: { bold: true, color: "#9A3412" },
  wrapText: true,
  verticalAlignment: "center",
};
guide.getRange("A10:E10").format.rowHeight = 40;

const rosterPreview = await workbook.render({
  sheetName: "등록양식",
  range: "A1:E8",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "등록양식_미리보기.png"), new Uint8Array(await rosterPreview.arrayBuffer()));
const guidePreview = await workbook.render({
  sheetName: "작성안내",
  range: "A1:E10",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "작성안내_미리보기.png"), new Uint8Array(await guidePreview.arrayBuffer()));

const inspection = await workbook.inspect({
  kind: "table",
  range: "등록양식!A1:E6",
  include: "values,formulas",
});
console.log(JSON.stringify(inspection, null, 2));
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(formulaErrors.ndjson);

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(outputDir, "우수강사명단등록.xlsx"));

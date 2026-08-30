import fs from "node:fs";

const html = fs.readFileSync("templates/certificate/excellent_form.html", "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
if (!scripts.length) {
  throw new Error("excellent_form.html에서 script 블록을 찾지 못했습니다.");
}
const source = scripts.at(-1)[1].replace(/\{\{[\s\S]*?\}\}/g, '"/mock"');
new Function(source);
console.log("excellent_form.html JavaScript syntax OK");

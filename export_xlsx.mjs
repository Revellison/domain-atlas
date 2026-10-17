// Convert the report's structured JSON into a filtered Excel workbook.
// Requires the @oai/artifact-tool runtime (available in Codex desktop).
import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const input = process.argv[2] || 'domains_report.details.json';
const output = process.argv[3] || 'domains_report.xlsx';
const previewPath = process.argv[4];
const data = JSON.parse(await fs.readFile(input, 'utf8'));
const wb = Workbook.create();
const sheet = wb.worksheets.add('Домены');
sheet.showGridLines = false;
sheet.tabColor = '#264563';
const headers = ['Домен', 'IP-адрес', 'Диапазон (BGP-префикс)', 'ASN / оператор сети', 'Сайт оператора', 'Резолв через'];
const literal = value => {
  const text = String(value ?? '');
  return /^\s*[=+@-]/.test(text) ? "'" + text : text;
};
const rows = data.records.map(row => [row.domain, row.ip || 'Не найден', row.prefix || 'Не определён',
  row.asn ? `AS${row.asn} — ${row.operator}` : row.status,
  row.website || 'Не найден', row.resolved_via || 'Не найден'].map(literal));
sheet.getRange('A2').values = [['Домены и сети размещения']];
sheet.getRange('A3:E3').values = [[
  `${data.domains} доменов`, `${data.rows} строк / ${data.distinct_asns} ASN`,
  'Группировка: оператор → ASN → домен', 'CDN указаны как операторы сети',
  `Проверено: ${new Date(data.generated_at_utc).toLocaleDateString('ru-RU',{timeZone:'Asia/Omsk'})}`
]];
sheet.getRange('C4:D4').values = [['Диапазон — BGP-префикс', 'Нет сайта в PeeringDB — «Не найден»']];
sheet.getRange('A5:F5').values = [headers];
const last = rows.length + 5;
if (rows.length) sheet.getRange(`A6:F${last}`).values = rows;
const used = sheet.getRange(`A2:F${last}`);
used.format.font = {name: 'Arial', size: 10, color: '#182B40'};
used.setNumberFormat('@');
used.format.verticalAlignment = 'center';
used.format.rowHeight = 26;
sheet.getRange('A2:E2').format.font = {name: 'Arial', size: 14, bold: true, color: '#182B40'};
sheet.getRange('A3:E4').format.font = {name: 'Arial', size: 10, color: '#526477'};
sheet.getRange('A3:E4').format.rowHeight = 22;
const widths = [32, 41, 37, 64, 60, 47];
for (let i=0;i<6;i++) sheet.getRangeByIndexes(4,i,last-4,1).format.columnWidth = widths[i];
sheet.getRange(`A6:F${last}`).format.wrapText = true;
rows.forEach((row,index) => {
  const lines = Math.max(...row.map((value,column) =>
    String(value).split('\n').reduce((count,line) => count + Math.max(1,Math.ceil(line.length / (widths[column]-3))),0)));
  sheet.getRange(`A${index+6}:F${index+6}`).format.rowHeight = Math.max(26,lines*14+8);
});
sheet.getRange('A5:F5').format = {
  fill:'#264563', font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},
  horizontalAlignment:'center',verticalAlignment:'center', rowHeight:30
};
const table = sheet.tables.add(`A5:F${last}`, true, 'DomainNetworks');
table.showFilterButton = true;
sheet.freezePanes.freezeRows(5);
// Separate organizations with a light line, keeping the ordered rows contiguous.
let previous = null;
data.records.forEach((record,index) => {
  const group = record.org_id ? `org:${record.org_id}` : `asn:${record.asn ?? 'unknown'}`;
  if (index && group !== previous) {
    sheet.getRange(`A${index+6}:F${index+6}`).format.borders = {
      top:{style:'thin',color:'#BAC9D8'}
    };
  }
  previous = group;
});
sheet.getRange(`E6:E${last}`).format.font.color = '#215C97';
// Sources stay in one place beside the output, outside the data table.
sheet.getRange('H3:H9').values = [
  ['Источники: Google DNS A, Team Cymru, PeeringDB, базы поддоменов'],
  ['https://www.team-cymru.com/ip-asn-mapping'],
  ['https://www.peeringdb.com/'],
  ['https://sslmate.com/help/reference/ct_search_api_v1'],
  ['https://github.com/ARPSyndicate/docs'],
  ['https://chaos.projectdiscovery.io/docs'],
  ['Имена из API проверены живым IPv4-резолвом.']
];
sheet.getRange('H3:H9').format.font = {name:'Arial',size:10,color:'#526477'};
sheet.getRange('H3:H9').format.columnWidth = 85;
sheet.getRange('F3:F4').values = [[`Через поддомены: ${data.domains_via_ct || 0} доменов`], ['«Резолв через» — фактическое имя хоста']];
sheet.getRange('F3:F4').format.font = {name:'Arial',size:10,color:'#526477'};
wb.recalculate();
const checked = await wb.inspect({kind:'region',sheetId:sheet.name,range:'A5:F10',maxChars:2000,tableMaxRows:6,tableMaxCols:6});
console.log(checked.ndjson ?? JSON.stringify(checked));
if (previewPath) {
  const preview = await wb.render({sheetName:sheet.name,range:'A1:F16',scale:1.3,format:'png'});
  await fs.mkdir(path.dirname(previewPath), {recursive:true});
  await fs.writeFile(previewPath,new Uint8Array(await preview.arrayBuffer()));
  let sample = -1;
  data.records.forEach((row,index) => {
    if (row.resolved_via && row.resolved_via !== row.domain &&
        (sample < 0 || row.resolved_via.length > data.records[sample].resolved_via.length)) sample = index;
  });
  if (sample >= 0) {
    const start = sample+6;
    const detail = await wb.render({sheetName:sheet.name,range:`A${start}:F${Math.min(last,start+5)}`,scale:1.3,format:'png'});
    const detailPath = previewPath.replace(/\.png$/i,'.subdomains.png');
    await fs.writeFile(detailPath,new Uint8Array(await detail.arrayBuffer()));
  }
}
await fs.mkdir(path.dirname(path.resolve(output)),{recursive:true});
const file = await SpreadsheetFile.exportXlsx(wb);
await file.save(output);
console.log(`Saved ${output}: ${rows.length} rows`);

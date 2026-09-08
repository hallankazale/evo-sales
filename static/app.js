const $ = (selector) => document.querySelector(selector);
let lastEventKey = '';

function cell(text, className = '') {
  const td = document.createElement('td');
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderEvents(events) {
  const log = $('#activity-log');
  if (!events.length) return;
  const key = JSON.stringify(events);
  if (key === lastEventKey) return;
  lastEventKey = key;
  log.replaceChildren();
  for (const event of events) {
    const item = document.createElement('div');
    item.className = 'activity-item';
    const dot = document.createElement('span');
    const text = document.createElement('p');
    const strong = document.createElement('strong');
    strong.textContent = `${event.time} — ${event.title}`;
    text.append(strong, document.createElement('br'), document.createTextNode(event.detail));
    item.append(dot, text);
    log.appendChild(item);
  }
}

async function loadDashboard() {
  const [metricsResponse, leadsResponse, healthResponse, agentResponse] = await Promise.all([
    fetch('/api/metrics'), fetch('/api/leads'), fetch('/api/health'), fetch('/api/agent'),
  ]);
  if (![metricsResponse, leadsResponse, healthResponse, agentResponse].every((r) => r.ok)) {
    throw new Error('Falha ao carregar dados');
  }

  const metrics = await metricsResponse.json();
  const leads = await leadsResponse.json();
  const health = await healthResponse.json();
  const agent = await agentResponse.json();

  $('#metric-leads').textContent = metrics.leads;
  $('#metric-qualified').textContent = metrics.qualified;
  $('#metric-messages').textContent = metrics.messages;
  $('#metric-clients').textContent = metrics.clients;
  $('#employee-status').textContent = agent.running ? 'Trabalhando' : 'Online';
  $('#employee-activity').textContent = health.activity;
  $('#mission-button').disabled = agent.running;
  $('#mission-button').textContent = agent.running ? 'EVO-01 trabalhando...' : 'Iniciar missão';
  renderEvents(agent.events);

  const body = $('#leads-body');
  body.replaceChildren();
  if (!leads.length) {
    const row = document.createElement('tr');
    const empty = cell('Nenhum lead ainda.', 'empty');
    empty.colSpan = 6;
    row.appendChild(empty);
    body.appendChild(row);
    return;
  }
  for (const lead of leads) {
    const row = document.createElement('tr');
    row.append(
      cell(`${lead.company_name} — ${lead.contact || 'Contato ainda não coletado'}`),
      cell(lead.segment || '—'), cell(lead.city || '—'), cell(String(lead.score), 'score'),
      cell(lead.status), cell(lead.message, 'message-cell'),
    );
    body.appendChild(row);
  }
}

$('#mission-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  payload.limit = Number(payload.limit || 5);
  const feedback = $('#mission-feedback');
  feedback.textContent = 'Enviando missão ao EVO-01...';
  try {
    const response = await fetch('/api/agent/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error('Não foi possível iniciar');
    feedback.textContent = 'Missão iniciada. Acompanhe a atividade ao lado.';
    await loadDashboard();
  } catch {
    feedback.textContent = 'Não foi possível iniciar a missão. Tente novamente.';
  }
});

$('#refresh-button').addEventListener('click', () => loadDashboard().catch(() => {}));

loadDashboard().catch(() => {
  $('#employee-status').textContent = 'Erro';
  $('#employee-activity').textContent = 'Falha ao carregar painel';
});
setInterval(() => loadDashboard().catch(() => {}), 2000);

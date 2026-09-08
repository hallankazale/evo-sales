const $ = (selector) => document.querySelector(selector);

function addActivity(title, detail) {
  const item = document.createElement('div');
  item.className = 'activity-item';

  const dot = document.createElement('span');
  const text = document.createElement('p');
  const strong = document.createElement('strong');
  strong.textContent = title;
  text.append(strong, document.createElement('br'), document.createTextNode(detail));
  item.append(dot, text);
  $('#activity-log').prepend(item);
}

function cell(text, className = '') {
  const td = document.createElement('td');
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

async function loadDashboard() {
  const [metricsResponse, leadsResponse, healthResponse] = await Promise.all([
    fetch('/api/metrics'),
    fetch('/api/leads'),
    fetch('/api/health'),
  ]);

  if (!metricsResponse.ok || !leadsResponse.ok || !healthResponse.ok) {
    throw new Error('Falha ao carregar dados');
  }

  const metrics = await metricsResponse.json();
  const leads = await leadsResponse.json();
  const health = await healthResponse.json();

  $('#metric-leads').textContent = metrics.leads;
  $('#metric-qualified').textContent = metrics.qualified;
  $('#metric-messages').textContent = metrics.messages;
  $('#metric-clients').textContent = metrics.clients;
  $('#employee-status').textContent = health.status === 'ok' ? 'Online' : 'Indisponível';
  $('#employee-activity').textContent = health.activity;

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
      cell(`${lead.company_name} — ${lead.contact || 'Sem contato'}`),
      cell(lead.segment || '—'),
      cell(lead.city || '—'),
      cell(String(lead.score), 'score'),
      cell(lead.status),
      cell(lead.message, 'message-cell'),
    );
    body.appendChild(row);
  }
}

$('#lead-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const feedback = $('#form-feedback');
  const payload = Object.fromEntries(new FormData(form).entries());

  feedback.textContent = 'EVO-01 analisando oportunidade...';
  $('#employee-activity').textContent = `Analisando ${payload.company_name}`;

  try {
    const response = await fetch('/api/leads', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!response.ok) throw new Error('Lead inválido');

    const lead = await response.json();
    feedback.textContent = `Lead analisado. Score: ${lead.score}/100.`;
    addActivity('Lead qualificado', `${lead.company_name} recebeu score ${lead.score}/100 e uma abordagem foi preparada.`);
    form.reset();
    form.elements.source.value = 'manual';
    await loadDashboard();
  } catch (error) {
    feedback.textContent = 'Não foi possível analisar esse lead. Confira os dados.';
  } finally {
    $('#employee-activity').textContent = 'Aguardando tarefa';
  }
});

$('#refresh-button').addEventListener('click', async () => {
  try {
    await loadDashboard();
    addActivity('Painel atualizado', 'Dados sincronizados com o EVO-01.');
  } catch {
    addActivity('Falha na atualização', 'Não foi possível sincronizar os dados agora.');
  }
});

loadDashboard().catch(() => {
  $('#employee-status').textContent = 'Erro';
  $('#employee-activity').textContent = 'Falha ao carregar painel';
});

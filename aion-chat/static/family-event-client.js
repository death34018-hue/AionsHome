(function (globalScope, factory) {
  const api = factory(globalScope);
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  globalScope.FamilyEventClient = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (globalScope) {
  async function reportFamilyEvent(kind, subjectName, fetchImpl) {
    const doFetch = fetchImpl || globalScope.fetch.bind(globalScope);
    const response = await doFetch('/api/family-events', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        kind,
        subject_name: String(subjectName || '').trim(),
      }),
    });
    if (!response.ok) throw new Error(`Family event report failed: ${response.status}`);
    return response.json();
  }

  return { reportFamilyEvent };
});

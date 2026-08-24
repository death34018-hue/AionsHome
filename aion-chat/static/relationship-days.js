(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.relationshipDays = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';

  const ONE_DAY = 24 * 60 * 60 * 1000;

  function localDateIso(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, '0');
    const day = String(date.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  function getRelationshipAnniversary(startedOn, today = new Date()) {
    const configured = /^\d{4}-\d{2}-\d{2}$/.test(String(startedOn || ''));
    const effectiveDate = configured ? String(startedOn) : localDateIso(today);
    const [year, month, day] = effectiveDate.split('-').map(Number);
    const startDay = Date.UTC(year, month - 1, day);
    const currentDay = Date.UTC(today.getFullYear(), today.getMonth(), today.getDate());
    const since = `${year}.${String(month).padStart(2, '0')}.${String(day).padStart(2, '0')}`;
    return {
      day: Math.max(0, Math.floor((currentDay - startDay) / ONE_DAY) + 1),
      since,
      configured,
      startedOn: effectiveDate,
    };
  }

  function buildRelationshipAnniversary(startedOn, name, today = new Date()) {
    const anniversary = getRelationshipAnniversary(startedOn, today);
    const displayName = String(name || 'AI').trim() || 'AI';
    return {
      name: displayName,
      eyebrow: '✦ OUR DAYS ✦',
      lead: '在一起的第',
      day: anniversary.day,
      unit: '天',
      since: `始于 ${anniversary.since}`,
      ariaLabel: `${displayName}空间：在一起的第${anniversary.day}天，始于${anniversary.since}`,
      configured: anniversary.configured,
      startedOn: anniversary.startedOn,
    };
  }

  return { buildRelationshipAnniversary, getRelationshipAnniversary, localDateIso };
});

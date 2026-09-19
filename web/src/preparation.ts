import { canonicalDeviceId, type Device } from './domain';

export type ResumeTarget = Pick<Device, 'id' | 'phone'> & { egress: 'proxy' | 'direct'; proxyId: string };
export type PreparationDialog = { target: ResumeTarget | null } | null;
export type PreparationEvent = { type: 'new' } | { type: 'close' } | { type: 'resume'; target: ResumeTarget };
export function preparationDialog(_state: PreparationDialog, event: PreparationEvent): PreparationDialog {
  if (event.type === 'close') return null;
  if (event.type === 'new') return { target: null };
  if (!canonicalDeviceId(event.target.id)) throw new Error('شناسهٔ دستگاه برای ادامهٔ آماده‌سازی معتبر نیست.');
  return { target: { ...event.target } };
}

export type PreparationValues = { phone: string; owner_authorized: boolean; artifact_id: string;
  egress: 'proxy' | 'direct'; proxy_id?: string };
// The selected device remains explicit; the host must still verify the original
// phone, network and application before continuing the existing preparation.
export function preparationRequest(dialog: PreparationDialog, values: PreparationValues) {
  if (!dialog) throw new Error('ابتدا فرم آماده‌سازی را باز کنید.');
  if (dialog.target && (values.egress !== dialog.target.egress ||
      (values.egress === 'proxy' && values.proxy_id !== dialog.target.proxyId))) {
    throw new Error('مسیر شبکهٔ ادامهٔ آماده‌سازی باید همان درخواست اولیه باشد.');
  }
  const params: Record<string, unknown> = { phone: values.phone, owner_authorized: values.owner_authorized,
    artifact_id: values.artifact_id, egress: values.egress,
    ...(values.egress === 'proxy' ? { proxy_id: values.proxy_id } : {}) };
  return { action: dialog.target ? 'resume' : 'provision', device: dialog.target?.id, params };
}

export const preparationTitle = (dialog: NonNullable<PreparationDialog>) => dialog.target ?
  `ادامهٔ آماده‌سازی ${dialog.target.id}` : 'آماده‌سازی دستگاه';

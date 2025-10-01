import { json, type ActionFunctionArgs } from '@remix-run/cloudflare';

let lastExternalSendData: any = null;

export async function action({ request }: ActionFunctionArgs) {
  const body = await request.json();
  lastExternalSendData = body;
  return json({ success: true });
}

export async function loader() {
  // 返回最新一次 POST 的内容，并清空（只消费一次）
  const data = lastExternalSendData;
  lastExternalSendData = null;
  return json({ data });
} 
import React from 'react';
import { Download, ExternalLink, X } from 'lucide-react';

export interface ImagePreviewData {
  /** 图片地址（后台鉴权接口 /api/admin/uploads/<ref>） */
  url: string;
  title: string;
  memberName?: string;
  time?: string;
  type?: string;
  note?: string;
}

interface Props {
  data: ImagePreviewData | null;
  onClose: () => void;
}

/**
 * 凭证图片大图预览。
 *
 * 纪律：
 * - 图片走后台鉴权接口（cookie 同源），⛔ 不走静态目录
 * - 下载用 <a download>，⛔ 不在前端做任何转码
 * - 图片加载失败如实提示「已过保留期」，⛔ 不显示破图
 */
export const ImagePreviewModal: React.FC<Props> = ({ data, onClose }) => {
  const [broken, setBroken] = React.useState(false);
  React.useEffect(() => setBroken(false), [data?.url]);

  if (!data) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4">
      <div className="w-full max-w-4xl bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden flex flex-col max-h-[92vh]">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-start justify-between gap-3">
          <div>
            <h3 className="font-bold text-slate-900 text-sm">{data.title}</h3>
            <div className="flex flex-wrap items-center gap-2 mt-1 text-[11px] text-slate-500">
              {data.memberName && (
                <span className="px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 font-medium">
                  提报人：{data.memberName}
                </span>
              )}
              {data.type && (
                <span className="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 font-medium">
                  {data.type}
                </span>
              )}
              {data.time && <span className="font-mono">{data.time}</span>}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors cursor-pointer"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 bg-slate-50 flex-1 overflow-auto flex items-center justify-center min-h-[240px]">
          {broken ? (
            <div className="text-center text-slate-500 text-xs space-y-1 py-10">
              <p className="font-semibold text-slate-700">图片暂时无法显示</p>
              <p>凭证图片只在保留期内可查看（提交后 48 小时内）。</p>
            </div>
          ) : (
            <img
              src={data.url}
              alt={data.title}
              onError={() => setBroken(true)}
              className="max-h-[62vh] max-w-full object-contain rounded-xl shadow-sm bg-white"
            />
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <span className="text-[11px] text-slate-500">{data.note || '提报凭证原图'}</span>
          <div className="flex items-center gap-2">
            <a
              href={data.url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg border border-slate-200 text-slate-700 text-xs font-semibold hover:bg-slate-50 transition-colors"
            >
              <ExternalLink className="w-3.5 h-3.5" />
              <span>新窗口打开</span>
            </a>
            <a
              href={data.url}
              download
              className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              <span>下载原图</span>
            </a>
          </div>
        </div>
      </div>
    </div>
  );
};

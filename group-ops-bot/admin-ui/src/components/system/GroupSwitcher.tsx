import React, { useMemo, useState } from 'react';
import { Search, ChevronDown, Check, X } from 'lucide-react';
import type { GroupItem } from '../../lib/types';

interface GroupSwitcherProps {
  icon: React.ReactNode;
  groups: GroupItem[];
  activeId: string;
  subtitle: string;
  onSelect: (group: GroupItem) => void;
  actions?: React.ReactNode;
  toast?: (message: string) => void;
}

/**
 * 统一群聊信息大屏卡片（仪表盘 / 群管理 / 统计中心三页一致）。
 * 右侧「切换群聊」为即时模糊搜索下拉，支持按群名或群 ID 检索。
 */
export const GroupSwitcher: React.FC<GroupSwitcherProps> = ({
  icon,
  groups,
  activeId,
  subtitle,
  onSelect,
  actions,
  toast,
}) => {
  const [open, setOpen] = useState(false);
  const [keyword, setKeyword] = useState('');
  const active = groups.find((g) => g.group_id === activeId);

  const filtered = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    if (!query) return groups;
    return groups.filter(
      (g) => (g.group_name || '').toLowerCase().includes(query)
        || g.group_id.includes(query),
    );
  }, [groups, keyword]);

  return (
    <div className="bg-white rounded-2xl p-4 border border-slate-200/90 shadow-2xs flex flex-wrap items-center justify-between gap-3 relative">
      <div className="flex items-center gap-3">
        <div className="w-10 h-10 rounded-xl bg-blue-50 border border-blue-200 text-blue-600 flex items-center justify-center shrink-0">
          {icon}
        </div>
        <div>
          <div className="flex items-center gap-2">
            <h3 className="font-bold text-slate-900 text-base">
              {active?.group_name || '暂无群聊'}
            </h3>
            {active && (
              <span className="text-[10px] text-slate-400 font-mono">
                ID: {active.group_id}
              </span>
            )}
            {active?.status === 'disabled' && (
              <span className="text-[10px] text-slate-500 bg-slate-100 border border-slate-200 px-1.5 py-0.5 rounded">
                已停用
              </span>
            )}
          </div>
          <p className="text-xs text-slate-500 mt-0.5">{subtitle}</p>
        </div>
      </div>

      <div className="flex items-center gap-2">
        <div className="relative">
          <button
            onClick={() => {
              setOpen((prev) => !prev);
              setKeyword('');
            }}
            className="px-3.5 py-2 text-xs border border-slate-300 hover:border-blue-500 rounded-xl text-slate-700 bg-white hover:bg-slate-50 flex items-center gap-2 transition-all cursor-pointer font-medium shadow-2xs"
          >
            <Search className="w-3.5 h-3.5 text-slate-400" />
            <span>切换群聊 ({groups.length})</span>
            <ChevronDown className="w-3.5 h-3.5 text-slate-400" />
          </button>

          {open && (
            <div className="absolute right-0 top-full mt-2 w-80 sm:w-96 bg-white rounded-2xl shadow-2xl border border-slate-200 p-3 z-50 text-xs animate-in fade-in zoom-in-95">
              <div className="relative mb-2">
                <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  type="text"
                  autoFocus
                  placeholder="输入群名称或群ID快速检索..."
                  value={keyword}
                  onChange={(e) => setKeyword(e.target.value)}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl pl-8 pr-7 py-2 text-xs text-slate-800 outline-none focus:border-blue-500 focus:bg-white"
                />
                {keyword && (
                  <button
                    onClick={() => setKeyword('')}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 cursor-pointer"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>

              <div className="max-h-72 overflow-y-auto space-y-1 pr-1">
                {filtered.length === 0 ? (
                  <div className="py-6 text-center text-slate-400">
                    <p>未找到匹配的群聊</p>
                    <span className="text-[10px]">支持输入群名称或数字ID搜索</span>
                  </div>
                ) : (
                  filtered.map((group) => {
                    const isSelected = group.group_id === activeId;
                    return (
                      <button
                        key={group.group_id}
                        onClick={() => {
                          onSelect(group);
                          setOpen(false);
                          toast?.(`已切换至【${group.group_name}】`);
                        }}
                        className={`w-full text-left p-2.5 rounded-xl transition-all cursor-pointer flex items-center justify-between ${
                          isSelected
                            ? 'bg-blue-50/80 text-blue-900 border border-blue-200 font-semibold'
                            : 'hover:bg-slate-50 text-slate-700 border border-transparent'
                        }`}
                      >
                        <div className="flex-1 pr-2 truncate">
                          <div className="truncate font-medium">{group.group_name}</div>
                          <div className="text-[10px] text-slate-400 font-mono mt-0.5">
                            ID: {group.group_id}
                          </div>
                        </div>
                        {isSelected && <Check className="w-4 h-4 text-blue-600 shrink-0" />}
                      </button>
                    );
                  })
                )}
              </div>

              <div className="pt-2 border-t border-slate-100 text-[10px] text-slate-400 flex justify-between items-center px-1">
                <span>支持模糊搜索即搜即达</span>
                <span>共 {filtered.length} 个群聊</span>
              </div>
            </div>
          )}
        </div>
        {actions}
      </div>
    </div>
  );
};

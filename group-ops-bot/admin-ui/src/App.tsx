import React, { useState } from 'react';
import { NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Link2,
  Users,
  BarChart3,
  ChevronRight,
} from 'lucide-react';
import { AppProvider } from './lib/store';
import { Dashboard } from './components/system/Dashboard';
import { RobotInstance } from './components/system/RobotInstance';
import { GroupManagement } from './components/system/GroupManagement';
import { Statistics } from './components/system/Statistics';
import {
  AdminUserCard,
  ChangePasswordModal,
  type AdminMe,
} from './components/system/AdminUserCard';

const MENU = [
  { to: '/', name: '仪表盘', icon: LayoutDashboard, end: true },
  { to: '/robot-instances', name: '群实例绑定', icon: Link2, end: false },
  { to: '/groups', name: '群管理', icon: Users, end: false },
  { to: '/stats', name: '统计中心', icon: BarChart3, end: false },
];

export const App: React.FC<{ me: AdminMe }> = ({ me }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const [forcedOpen, setForcedOpen] = useState(me.must_change_password);

  return (
    <AppProvider me={me}>
      <div className="w-full min-h-screen md:h-screen bg-[#f8fafc] flex flex-col md:flex-row md:overflow-hidden">
        {/* 左侧导航（贴边工作台布局；桌面端锁定屏高、不可随页面滚动） */}
        <aside className="w-full md:w-60 md:h-full bg-white border-r border-slate-200/90 p-4 flex flex-col justify-between shrink-0 select-none md:overflow-hidden">
          <div className="space-y-6">
            <div className="flex items-center gap-2.5 px-2 py-1.5 border-b border-slate-100 pb-3">
              <div className="w-9 h-9 rounded-xl bg-blue-600 flex items-center justify-center shadow-xs">
                <svg viewBox="0 0 24 24" className="w-5 h-5 text-white" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="8" width="18" height="12" rx="3" />
                  <path d="M12 4v4" />
                  <circle cx="12" cy="3" r="1" />
                  <circle cx="9" cy="13" r="1" fill="currentColor" />
                  <circle cx="15" cy="13" r="1" fill="currentColor" />
                  <path d="M9.5 17h5" />
                </svg>
              </div>
              <div>
                <h2 className="font-bold text-sm text-slate-900 tracking-tight">群运维机器人</h2>
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-slate-400 font-mono">group-ops-bot</span>
                  <span className="text-[9px] text-blue-600 bg-blue-50 px-1 py-0.2 rounded font-medium">
                    工作台
                  </span>
                </div>
              </div>
            </div>

            <nav className="space-y-1.5 text-xs">
              {MENU.map((item) => {
                const Icon = item.icon;
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      `w-full flex items-center justify-between px-3 py-2.5 rounded-xl transition-all cursor-pointer ${
                        isActive
                          ? 'bg-blue-50 text-blue-700 font-bold shadow-2xs border-l-4 border-blue-600'
                          : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
                      }`
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <div className="flex items-center gap-2.5">
                          <Icon
                            className={`w-4 h-4 ${isActive ? 'text-blue-600' : 'text-slate-400'}`}
                          />
                          <span>{item.name}</span>
                        </div>
                        {isActive && <ChevronRight className="w-3.5 h-3.5 text-blue-600" />}
                      </>
                    )}
                  </NavLink>
                );
              })}
            </nav>
          </div>

          <div className="pt-4 border-t border-slate-100 space-y-3">
            <AdminUserCard
              me={me}
              onLoggedOut={() => {
                window.location.href = '/login';
              }}
            />
          </div>
        </aside>

        {/* 右侧主内容区（唯一可滚动区域） */}
        <main className="flex-1 bg-[#f7f8fa] p-4 lg:p-6 overflow-y-auto min-h-screen md:min-h-0 md:h-full">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route
              path="/robot-instances"
              element={
                <RobotInstance
                  key={`instances-${location.search}`}
                />
              }
            />
            <Route path="/groups" element={<GroupManagement />} />
            <Route path="/stats" element={<Statistics />} />
            <Route
              path="*"
              element={
                <div className="bg-white rounded-2xl border border-slate-200 p-10 text-center text-sm text-slate-500">
                  页面不存在。
                  <button
                    onClick={() => navigate('/')}
                    className="ml-1 text-blue-600 font-bold cursor-pointer"
                  >
                    返回仪表盘
                  </button>
                </div>
              }
            />
          </Routes>
        </main>
      </div>

      {forcedOpen && (
        <ChangePasswordModal
          isOpen={forcedOpen}
          forced
          onClose={() => setForcedOpen(false)}
          onChanged={() => setForcedOpen(false)}
        />
      )}
    </AppProvider>
  );
};

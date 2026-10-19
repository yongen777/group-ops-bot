import React from 'react';
import { ChangePasswordModal } from '../components/system/AdminUserCard';

/**
 * 强制改密页（must_change_password=true 时唯一可达页面）。
 * 复用侧栏同款改密弹窗的表单与校验，⛔ 不另起一套措辞。
 */
export const ChangePasswordPage: React.FC<{ onChanged: () => void }> = ({ onChanged }) => (
  <div className="min-h-screen w-full bg-[#f7f8fa]">
    <ChangePasswordModal isOpen forced onClose={() => undefined} onChanged={onChanged} />
  </div>
);

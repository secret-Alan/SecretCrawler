import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

void main() => runApp(const SecretCrawlerApp());

class SecretCrawlerApp extends StatelessWidget {
  const SecretCrawlerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'SecretCrawler',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        brightness: Brightness.light,
        colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff005f73)),
        fontFamily: 'Arial',
        scaffoldBackgroundColor: const Color(0xfff8f6f1),
        useMaterial3: true,
      ),
      home: const WorkspaceScreen(),
    );
  }
}

class WorkspaceScreen extends StatefulWidget {
  const WorkspaceScreen({super.key});

  @override
  State<WorkspaceScreen> createState() => _WorkspaceScreenState();
}

class _WorkspaceScreenState extends State<WorkspaceScreen> {
  final List<String> _browserTabs = ['首页', '任务', '连接'];
  int _activeBrowserTab = 0;
  String _connection = '本地服务未连接';

  Future<void> _checkServer() async {
    try {
      final response = await http.get(Uri.parse('http://127.0.0.1:8080/health'));
      final data = jsonDecode(response.body) as Map<String, dynamic>;
      if (!mounted) return;
      setState(() => _connection = '本地服务：${data['status']}');
    } catch (_) {
      if (!mounted) return;
      setState(() => _connection = '本地服务不可用');
    }
  }

  void _newBrowserTab() {
    setState(() {
      _browserTabs.add('新任务 ${_browserTabs.length + 1}');
      _activeBrowserTab = _browserTabs.length - 1;
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 1280),
            child: Padding(
              padding: const EdgeInsets.all(18),
              child: DecoratedBox(
                decoration: BoxDecoration(
                  border: Border.all(color: const Color(0xff202020), width: 1.3),
                  color: const Color(0xfffffdfa),
                ),
                child: Column(
                  children: [
                    _WindowHeader(connection: _connection, onHealthCheck: _checkServer),
                    _WorkspaceTabs(
                      tabs: _browserTabs,
                      activeIndex: _activeBrowserTab,
                      onSelect: (index) => setState(() => _activeBrowserTab = index),
                      onClose: _browserTabs.length == 1
                          ? null
                          : (index) => setState(() {
                                _browserTabs.removeAt(index);
                                _activeBrowserTab = _activeBrowserTab.clamp(0, _browserTabs.length - 1) as int;
                              }),
                      onAdd: _newBrowserTab,
                    ),
                    const Divider(height: 1, color: Color(0xff202020)),
                    Expanded(
                      child: Row(
                        children: [
                          Expanded(flex: 37, child: _Sidebar(onHealthCheck: _checkServer)),
                          const VerticalDivider(width: 1, color: Color(0xff202020)),
                          const Expanded(flex: 63, child: _Canvas()),
                        ],
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _WindowHeader extends StatelessWidget {
  const _WindowHeader({required this.connection, required this.onHealthCheck});

  final String connection;
  final VoidCallback onHealthCheck;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 38,
      child: Row(
        children: [
          const Padding(padding: EdgeInsets.symmetric(horizontal: 10), child: Text('SecretCrawler', style: TextStyle(fontWeight: FontWeight.w700))),
          const Spacer(),
          TextButton(onPressed: onHealthCheck, child: Text(connection, style: const TextStyle(fontSize: 11))),
          const _WindowControl(icon: Icons.minimize),
          const _WindowControl(icon: Icons.crop_square_outlined),
          const _WindowControl(icon: Icons.close),
        ],
      ),
    );
  }
}

class _WindowControl extends StatelessWidget {
  const _WindowControl({required this.icon});
  final IconData icon;

  @override
  Widget build(BuildContext context) => SizedBox(width: 30, child: Icon(icon, size: 16));
}

class _WorkspaceTabs extends StatelessWidget {
  const _WorkspaceTabs({required this.tabs, required this.activeIndex, required this.onSelect, required this.onClose, required this.onAdd});
  final List<String> tabs;
  final int activeIndex;
  final ValueChanged<int> onSelect;
  final ValueChanged<int>? onClose;
  final VoidCallback onAdd;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 38,
      child: Row(
        children: [
          const SizedBox(width: 10),
          for (var index = 0; index < tabs.length; index++)
            _BrowserTab(label: tabs[index], active: index == activeIndex, onTap: () => onSelect(index), onClose: onClose == null ? null : () => onClose!(index)),
          IconButton(onPressed: onAdd, icon: const Icon(Icons.add), iconSize: 20, tooltip: '新建标签'),
        ],
      ),
    );
  }
}

class _BrowserTab extends StatelessWidget {
  const _BrowserTab({required this.label, required this.active, required this.onTap, this.onClose});
  final String label;
  final bool active;
  final VoidCallback onTap;
  final VoidCallback? onClose;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: Container(
        height: 31,
        width: 155,
        margin: const EdgeInsets.only(right: 3),
        padding: const EdgeInsets.only(left: 11, right: 3),
        decoration: BoxDecoration(color: active ? const Color(0xfffffdfa) : const Color(0xffe8eceb), border: Border.all(color: const Color(0xff202020)), borderRadius: const BorderRadius.vertical(top: Radius.circular(6))),
        child: Row(children: [Expanded(child: Text(label, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12))), if (onClose != null) InkResponse(onTap: onClose, radius: 14, child: const Icon(Icons.close, size: 15))]),
      ),
    );
  }
}

class _Sidebar extends StatelessWidget {
  const _Sidebar({required this.onHealthCheck});
  final VoidCallback onHealthCheck;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Icon(Icons.menu, size: 20, color: Color(0xff202020)),
          const SizedBox(height: 10),
          const Divider(color: Color(0xffbdc9c7), height: 1),
          const SizedBox(height: 14),
          const _TaskCard(),
          const Spacer(),
          _ModelPanel(onHealthCheck: onHealthCheck),
        ],
      ),
    );
  }
}

class _TaskCard extends StatelessWidget {
  const _TaskCard();
  @override
  Widget build(BuildContext context) => Container(
        height: 142,
        decoration: BoxDecoration(border: Border.all(color: const Color(0xff202020), width: 1.3), borderRadius: BorderRadius.circular(9)),
        child: const Padding(padding: EdgeInsets.all(14), child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text('任务概览', style: TextStyle(fontWeight: FontWeight.w700)), SizedBox(height: 10), Text('授权端点  0\n等待审批  0', style: TextStyle(fontSize: 12, height: 1.8))])),
      );
}

class _ModelPanel extends StatelessWidget {
  const _ModelPanel({required this.onHealthCheck});
  final VoidCallback onHealthCheck;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.fromLTRB(12, 10, 7, 7),
        decoration: BoxDecoration(border: Border.all(color: const Color(0xff202020), width: 1.3), borderRadius: BorderRadius.circular(9)),
        child: Row(children: [const Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [Text('Kimi K3', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 12)), Text('本地优先 · 需授权', style: TextStyle(fontSize: 10))])), const Icon(Icons.keyboard_arrow_up, size: 16), IconButton(onPressed: onHealthCheck, icon: const Icon(Icons.power_settings_new), iconSize: 17, tooltip: '检查本地服务')]),
      );
}

class _Canvas extends StatelessWidget {
  const _Canvas();
  @override
  Widget build(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const _InspectorTabs(),
        const Expanded(child: Center(child: _EmptyWorkspace())),
      ]);
}

class _InspectorTabs extends StatelessWidget {
  const _InspectorTabs();
  @override
  Widget build(BuildContext context) => SizedBox(height: 34, child: Row(children: const [SizedBox(width: 10), _InspectorTab(label: '参数', active: true), _InspectorTab(label: '审批'), _InspectorTab(label: '网络') ]));
}

class _InspectorTab extends StatelessWidget {
  const _InspectorTab({required this.label, this.active = false});
  final String label;
  final bool active;
  @override
  Widget build(BuildContext context) => Container(width: 118, margin: const EdgeInsets.only(right: 4), alignment: Alignment.centerLeft, padding: const EdgeInsets.symmetric(horizontal: 10), decoration: BoxDecoration(color: active ? const Color(0xfffffdfa) : const Color(0xffedf0ef), border: Border.all(color: const Color(0xff202020)), borderRadius: const BorderRadius.vertical(top: Radius.circular(5))), child: Text(label, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600)));
}

class _EmptyWorkspace extends StatelessWidget {
  const _EmptyWorkspace();
  @override
  Widget build(BuildContext context) => const Column(mainAxisSize: MainAxisSize.min, children: [Icon(Icons.account_tree_outlined, size: 36, color: Color(0xff6a7774)), SizedBox(height: 10), Text('选择已获授权的端点以开始创建任务', style: TextStyle(color: Color(0xff52615e))), SizedBox(height: 4), Text('所有敏感操作均需要可审计的人工审批。', style: TextStyle(fontSize: 12, color: Color(0xff7a8783)))]);
}

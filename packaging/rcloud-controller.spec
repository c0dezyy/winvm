# Build:  pyinstaller packaging/rcloud-controller.spec
block_cipher = None
a = Analysis(['../rcloud/cli.py'], pathex=['..'], datas=[('../dashboard/index.html', 'dashboard')])
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
         name='rcloud-controller', console=True, onefile=True)

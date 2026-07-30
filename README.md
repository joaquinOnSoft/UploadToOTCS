# Upload to Opentext Content Management

Script de subida de ficheros (.txt y .pdf) a un nodo
de OpenText Content Management (xECM / Extended ECM) mediante la API REST,
asignando la categoría "Raw material" con los atributos "Name" y "Ticker".

Parámetros:

 - `--input INPUT` o `--i INPUT` : Carpeta local desde la que se leen los ficheros .txt o .pdf
 - `--node NODE` o `-o NODE`: ID del nodo raíz de xECM donde se subirán los archivos

Uso:

```shell
    python upload_to_otcs.py --input ./ficheros --node 123456 
    python upload_to_otcs.py -i ./ficheros -n 123456
```